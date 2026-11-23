"""Pure, adaptive event-boundary simulation built on the registered RuleEngine."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid5

from world_engine.agents import AgentPolicy, AmbiguousGoalPriority
from world_engine.domain import DomainEvent, GovernanceSuccessionEvent, LifeEndedEvent, WorldState
from world_engine.domain.components import (
    GovernanceComponent, InventoryComponent, LifeComponent, NeedsComponent, ScheduledEventRecord,
    leadership_office_ids_held_by,
)
from world_engine.domain.gameplay_events import BackgroundNeedsAdvancedEvent, ComponentReplacedEvent
from world_engine.domain.events import parse_event
from world_engine.domain.market_pricing import PRICE_REVIEW_HANDLER_ID, PRICE_REVIEW_HANDLER_VERSION
from world_engine.domain.story_nodes import STORY_NODE_HANDLER, STORY_NODE_VERSION
from world_engine.domain.institutional_credit_state import CREDIT_MATURITY_HANDLER, CREDIT_MATURITY_HANDLER_VERSION
from world_engine.domain.models import TimeAdvancedEvent
from world_engine.domain.needs import PASSIVE_NEED_RATES_PER_DAY, next_need_threshold_tick
from world_engine.domain.work_shift import WorkShiftError, WorkShiftTerms, registered_work_shift_terms, select_wage_holder_account
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.domain.npc_attendance import NpcAttendance, npc_attendance_is_valid, plan_npc_attendance_start
from world_engine.domain.npc_attendance_events import (
    NpcAttendanceCancelledEvent, NpcAttendanceSettledEvent, NpcAttendanceStartedEvent, npc_attendance_event_id,
)
from world_engine.domain.npc_goal_completion import npc_goal_fulfillment_allows_continuation
from world_engine.domain.npc_work_shift import plan_attended_work_shift
from world_engine.domain.npc_plans import NpcTimedExecution
from world_engine.domain.relationship_promises import PROMISE_TRUST_DEADLINE_HANDLER_VERSION
from world_engine.domain.npc_commitments import MEETING_COMMITMENT_HANDLER, MEETING_COMMITMENT_VERSION
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID, TRAVEL_RESOLUTION_HANDLER_VERSION, active_travel_journey, travel_resolution_schedule_id,
)
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.domain.reducers import apply_event, state_json, state_payload_hash
from world_engine.rules.contracts import RuleContext, RuleOutcome, RuleResolution
from world_engine.rules import BackgroundAgentChoice
from world_engine.rules.engine import RULES_VERSION, RuleEngine, _observed_entities
from world_engine.rules.itinerary_care_validation import (
    _canonical_model_serialization,
    _ordered_prestate_digest,
    evaluate_itinerary_care,
)
from world_engine.rules.contracts import ConsumeIntent, MoveIntent, ProduceIntent, RestIntent, TrainIntent, WaitIntent, WorkShiftIntent

from .contracts import (
    DecisionOption,
    DecisionRequest,
    DecisionResponse,
    GovernanceDecisionTarget,
    Interruption,
    LongActionPlan,
    ResolutionMetrics,
    SimulationPlan,
    SimulationContext,
    SimulationResolution,
    StopCondition,
    WorkPurchaseAffordabilityReceipt,
    WorkPurchaseTarget,
    WorldAdvancePlan,
)
from .npc_work_schedule import NpcWorkTransition, next_npc_work_tick, npc_work_transition
from .npc_routine_departure import plan_npc_routine_departure
from .npc_routine_schedule import next_npc_routine_tick, npc_routine_candidate
from .npc_meeting_attendance import (
    NPC_MEETING_ATTENDANCE_DECISION_KIND,
    next_npc_meeting_attendance_boundary,
    next_npc_meeting_attendance_candidate,
    npc_meeting_attendance_request,
)
from .work_purchase import observe_work_purchase_quote


class RuleEvaluator(Protocol):
    """Injectable rule boundary for tests and explicit rule-package composition."""

    def evaluate(self, state, intent, context: RuleContext) -> RuleResolution: ...

    def resolve_scheduled(
        self, state: WorldState, scheduled: ScheduledEventRecord, context: RuleContext
    ) -> RuleResolution: ...


class StaleDecisionResponse(ValueError):
    """A model decision no longer matches the world snapshot that requested it."""


@dataclass(frozen=True, slots=True)
class SimulationPolicy:
    """Parameters for NPC candidate selection, separate from speed and depth."""

    rest_when_below: int = 30
    eat_when_below: int = 25
    max_macro_step_seconds: int = 31_622_400

    def __post_init__(self) -> None:
        if not 0 <= self.rest_when_below <= 100 or not 0 <= self.eat_when_below <= 100:
            raise ValueError("need thresholds must be in 0..100")
        if self.max_macro_step_seconds < 60:
            raise ValueError("max_macro_step_seconds must be at least one minute")


_ITINERARY_TRANSIT_PROJECTION_CAPABILITY = object()


def _is_ordinary_itinerary_transit_batch(
    events: tuple[DomainEvent, ...], *, actor_id: UUID, to_tick: int,
) -> bool:
    """Allow the bounded clock/background/focal-needs transit event shape."""

    if not 1 <= len(events) <= 3 or type(events[0]) is not TimeAdvancedEvent or events[0].to_tick != to_tick:
        return False
    index = 1
    if index < len(events) and type(events[index]) is BackgroundNeedsAdvancedEvent:
        if events[index].actor_id != actor_id:
            return False
        index += 1
    if index < len(events) and type(events[index]) is ComponentReplacedEvent:
        event = events[index]
        if event.component_name != "needs" or event.entity_id != actor_id:
            return False
        index += 1
    return index == len(events)


@dataclass(frozen=True, slots=True)
class _ItineraryTransitProjectionReceipt:
    """Ephemeral proof for one exact native, uninterrupted transit window."""

    world_id: UUID
    branch_id: UUID
    job_id: UUID
    worker_id: str
    fencing_token: int
    state_version: int
    command_id: UUID
    actor_id: UUID
    itinerary_journey_id: UUID
    route_id: UUID
    ruleset_version: str
    prestate_hash: str
    prestate_ordered_digest: str
    event_serializations: tuple[str, ...]
    event_ticks: tuple[int, ...]
    projected_payload: dict[str, object] = field(repr=False, compare=False)
    projected_state_hash: str
    projected_state_ordered_digest: str
    projected_current_tick: int
    _capability: object = field(repr=False, compare=False)

    def matches_execution(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        job_id: UUID,
        worker_id: str,
        fencing_token: int,
        state_version: int,
        command_id: UUID,
        actor_id: UUID,
        itinerary_journey_id: UUID,
        route_id: UUID,
        ruleset_version: str,
        prestate_hash: str,
    ) -> bool:
        return (
            self._capability is _ITINERARY_TRANSIT_PROJECTION_CAPABILITY
            and self.world_id == world_id
            and self.branch_id == branch_id
            and self.job_id == job_id
            and self.worker_id == worker_id
            and self.fencing_token == fencing_token
            and self.state_version == state_version
            and self.command_id == command_id
            and self.actor_id == actor_id
            and self.itinerary_journey_id == itinerary_journey_id
            and self.route_id == route_id
            and self.ruleset_version == ruleset_version
            and self.prestate_hash == prestate_hash
        )

    def matches_events(self, events: tuple[DomainEvent, ...]) -> bool:
        return (
            self._capability is _ITINERARY_TRANSIT_PROJECTION_CAPABILITY
            and self.event_serializations == tuple(_canonical_model_serialization(event) for event in events)
        )

    def matches_prestate(self, state: WorldState) -> bool:
        return (
            self._capability is _ITINERARY_TRANSIT_PROJECTION_CAPABILITY
            and self.prestate_ordered_digest == _ordered_prestate_digest(state)
        )

    def matches_projected_payload(self, payload: dict[str, object]) -> bool:
        return (
            self._capability is _ITINERARY_TRANSIT_PROJECTION_CAPABILITY
            and self.projected_state_ordered_digest == _ordered_prestate_digest(payload)
        )


class SimulationEngine:
    """Propose one validated event batch, without persistence or state mutation.

    Ordinary rest, eating, movement, training, and production are resolved by the
    domain RuleEngine. This class chooses a safe interval, stops at known event or
    life boundaries, validates decision freshness, and audits the returned batch.
    """

    def __init__(
        self,
        *,
        rule_engine: RuleEvaluator | None = None,
        agent_policy: AgentPolicy | None = None,
        policy: SimulationPolicy | None = None,
        npc_capability_registry=None,
    ) -> None:
        self.rule_engine = rule_engine or RuleEngine()
        self._native_rule_engine = type(self.rule_engine) is RuleEngine
        self.agent_policy = agent_policy or AgentPolicy(
            rest_when_below=(policy or SimulationPolicy()).rest_when_below,
            eat_when_below=(policy or SimulationPolicy()).eat_when_below,
        )
        self.policy = policy or SimulationPolicy()
        self.npc_capability_registry = npc_capability_registry

    def _eligible_itinerary_transit_projection(
        self, state: WorldState, plan: SimulationPlan, context: SimulationContext, *,
        activity: str, reason: str, reference: UUID | None, subject: UUID | None,
    ) -> bool:
        binding = context._itinerary_transit_projection_binding
        if (
            not self._native_rule_engine
            or not isinstance(plan, LongActionPlan)
            or plan.activity != "travel"
            or plan.route_id is None
            or activity != "travel"
            or reason not in {"travel_in_progress", "itinerary_auto_care_threshold"}
            or reference is not None
            or subject != plan.actor_id
            or context.itinerary_journey_id is None
            or context.decision_responses
            or not isinstance(binding, tuple)
            or len(binding) != 4
            or not isinstance(binding[0], UUID)
            or not isinstance(binding[1], str)
            or not isinstance(binding[2], int)
            or isinstance(binding[2], bool)
            or not isinstance(binding[3], str)
        ):
            return False
        journey = active_travel_journey(state, plan.actor_id)
        return bool(
            journey is not None
            and journey.journey_id == context.itinerary_journey_id
            and journey.route_id == plan.route_id
            and journey.status == "in_transit"
        )

    @staticmethod
    def _issue_itinerary_transit_projection_receipt(
        state: WorldState,
        context: SimulationContext,
        plan: LongActionPlan,
        resolution: RuleResolution,
        combined: RuleResolution,
        *,
        exact_events: tuple[DomainEvent, ...],
        boundary_tick: int,
        reference: UUID | None,
        subject: UUID | None,
    ) -> _ItineraryTransitProjectionReceipt | None:
        binding = context._itinerary_transit_projection_binding
        projected = resolution._validated_projection
        if (
            not isinstance(binding, tuple)
            or len(binding) != 4
            or not isinstance(projected, WorldState)
            or not exact_events
            or tuple(resolution.events) != exact_events
            or tuple(combined.events) != exact_events
            or combined.outcome != resolution.outcome
            or resolution.outcome.status != "succeeded"
            or resolution.interruptions
            or resolution.elapsed_ticks <= 0
            or state.current_tick + resolution.elapsed_ticks != boundary_tick
            or projected.current_tick != boundary_tick
            or resolution.ruleset_version != context.ruleset_version
            or context.itinerary_journey_id is None
            or plan.route_id is None
            or reference is not None
            or subject != plan.actor_id
            or not _is_ordinary_itinerary_transit_batch(
                exact_events, actor_id=plan.actor_id, to_tick=boundary_tick,
            )
        ):
            return None
        job_id, worker_id, fencing_token, prestate_hash = binding
        if (
            not isinstance(job_id, UUID)
            or not isinstance(worker_id, str)
            or type(fencing_token) is not int
            or fencing_token < 1
            or not isinstance(prestate_hash, str)
        ):
            return None
        payload = state_json(projected)
        return _ItineraryTransitProjectionReceipt(
            world_id=context.world_id,
            branch_id=context.branch_id,
            job_id=job_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            state_version=context.state_version,
            command_id=context.command_id,
            actor_id=plan.actor_id,
            itinerary_journey_id=context.itinerary_journey_id,
            route_id=plan.route_id,
            ruleset_version=resolution.ruleset_version,
            prestate_hash=prestate_hash,
            prestate_ordered_digest=_ordered_prestate_digest(state),
            event_serializations=tuple(_canonical_model_serialization(event) for event in exact_events),
            event_ticks=tuple(boundary_tick for _ in exact_events),
            projected_payload=payload,
            projected_state_hash=state_payload_hash(payload),
            projected_state_ordered_digest=_ordered_prestate_digest(payload),
            projected_current_tick=projected.current_tick,
            _capability=_ITINERARY_TRANSIT_PROJECTION_CAPABILITY,
        )

    def plan_window(
        self,
        state: WorldState,
        plan: SimulationPlan,
        context: SimulationContext,
    ) -> SimulationResolution:
        """Plan the next bounded interval and return a pure, replayable proposal."""

        if isinstance(plan, WorldAdvancePlan):
            return self._plan_world_advance(state, plan, context)

        if plan.consumption is not None and state.current_tick == plan.target_tick:
            self._reject_unconsumed_decisions(context)
            return self._empty(state, context, outcome="completed", reason="consumption_committed")

        due_now = self._next_external_boundary(
            state, state.current_tick, actor_id=plan.actor_id,
            externally_controlled_actor_ids=context.externally_controlled_actor_ids,
        )
        if due_now is not None:
            _tick, reason, reference_id, subject_id = due_now
            if reason == "npc_plan_due":
                return self._resolve_npc_plan_execution(state, plan, context, reference_id)
            if reason == "background_deprivation_pending":
                self._reject_unconsumed_decisions(context)
                return self._resolve_background_deprivation(state, plan, context, subject_id)
            scheduled = next((event for event in state.scheduled_events if event.id == reference_id), None) if reference_id else None
            if scheduled is not None:
                if scheduled.due_tick < state.current_tick:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(
                        state,
                        context,
                        outcome="rule_required",
                        reason="scheduled_event_overdue_reconciliation_required",
                        interruption=Interruption(
                            kind="rule_required",
                            tick=state.current_tick,
                            reason_code="scheduled_event_overdue_reconciliation_required",
                            reference_id=scheduled.id,
                        ),
                    )
                if scheduled.phase == "decision":
                    return self._decision_gate(
                        state, context, self._scheduled_decision_request(state, context, scheduled), continuation=plan
                    )
                return self._resolve_scheduled_event(state, scheduled, context, plan)
            return self._resolve_current_lifespan_boundary(state, context, subject_id, plan)

        succession_request = self._pending_succession_request(state, context)
        if succession_request is not None:
            return self._decision_gate(state, context, succession_request, continuation=plan)
        vacancy_resolution = self.resolve_autonomous_vacancy(state, plan, context)
        if vacancy_resolution is not None:
            return vacancy_resolution
        if context.work_job_id is not None and context.allow_npc_routine_mobility:
            work = npc_work_transition(state, excluded_actor_ids=self._npc_routine_exclusions(plan, context))
            if work is not None:
                self._reject_unconsumed_decisions(context)
                return self._resolve_npc_work(state, plan, context, work)
        attendance = next((row for row in state.npc_attendance if row.work_job_id is not None
                           and row.work_job_id == context.work_job_id), None)
        if attendance is not None:
            return self._advance_attended_work(state, plan, context, attendance)
        meeting_resolution = self.resolve_autonomous_meeting_attendance(state, plan, context)
        if meeting_resolution is not None:
            return meeting_resolution

        actor = state.entities.get(plan.actor_id)
        if actor is None or actor.kind not in {"character", "person"}:
            raise ValueError("Long-action actor must be a character in the current state.")
        if actor.status in {"dead", "retired"}:
            raise ValueError(f"A {actor.status} character cannot act.")
        if actor.status == "incapacitated":
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state, context, outcome="stopped", reason="actor_incapacitated",
                interruption=Interruption(kind="boundary", tick=state.current_tick,
                                          reason_code="actor_incapacitated", subject_id=actor.id),
            )
        if plan.consumption is not None:
            return self._execute_exact_consumption(state, plan, context)
        if plan.target_tick < state.current_tick:
            raise ValueError("Long-action target_tick cannot precede the current world tick.")
        if plan.started_tick is not None and plan.started_tick > state.current_tick:
            raise ValueError("Long-action started_tick cannot be in the future.")
        if self._stop_satisfied(state, plan.stop_conditions):
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state,
                context,
                outcome="stopped",
                reason="stop_condition_already_satisfied",
                interruption=Interruption(kind="stop_condition", tick=state.current_tick, reason_code="stop_condition_already_satisfied", subject_id=plan.actor_id),
            )

        work_terms: WorkShiftTerms | None = None
        work_account_id: UUID | None = None
        work_location_id: UUID | None = None
        next_work_shift_tick: int | None = None
        next_work_rest_tick: int | None = None
        next_work_rest_duration: int | None = None
        between_work_shifts = False
        work_shift_due = False
        split_work_shift = False
        if plan.activity == "work":
            work_details = self._routine_work_details(state, plan)
            if isinstance(work_details, str):
                self._reject_unconsumed_decisions(context)
                return self._empty(
                    state, context, outcome="stopped", reason=work_details,
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick,
                        reason_code=work_details, subject_id=plan.actor_id,
                        reference_id=plan.work_schedule.assignment_id if plan.work_schedule else None,
                    ),
                )
            work_terms, work_account_id, work_location_id = work_details
            assert plan.work_schedule is not None
            wage_account = state.resources[work_account_id]
            purchase_target = plan.work_schedule.purchase_target
            if purchase_target is not None:
                try:
                    quote = self._work_purchase_quote(
                        state, purchase_target, actor_id=plan.actor_id, workplace_id=work_location_id,
                        wage_resource_id=wage_account.resource_id,
                    )
                except ValueError:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(
                        state, context, outcome="stopped", reason="work_purchase_target_unavailable",
                        interruption=Interruption(
                            kind="rule_required", tick=state.current_tick,
                            reason_code="work_purchase_target_unavailable", subject_id=plan.actor_id,
                            reference_id=purchase_target.listing_id,
                        ),
                    )
                if wage_account.balance >= quote:
                    routine = self._resolve_npc_routine(state, plan, context)
                    if routine is not None:
                        return routine
                    self._reject_unconsumed_decisions(context)
                    return self._empty(
                        state, context, outcome="completed", reason="work_purchase_target_affordable",
                        work_purchase_affordability=WorkPurchaseAffordabilityReceipt(
                            listing_id=purchase_target.listing_id,
                            quantity=purchase_target.quantity,
                            currency_resource_id=purchase_target.currency_resource_id,
                            total_price_minor=quote,
                            wage_balance=wage_account.balance,
                            confirmed_tick=state.current_tick,
                        ),
                    )
            if (
                plan.work_schedule.balance_target is not None
                and wage_account.balance >= plan.work_schedule.balance_target
            ):
                routine = self._resolve_npc_routine(state, plan, context)
                if routine is not None:
                    return routine
                self._reject_unconsumed_decisions(context)
                return self._empty(
                    state, context, outcome="completed", reason="work_balance_target_reached",
                )
            cadence_ticks = plan.work_schedule.repeat_every_days * state.calendar.day_seconds
            if cadence_ticks < work_terms.duration_seconds:
                self._reject_unconsumed_decisions(context)
                return self._empty(
                    state, context, outcome="stopped", reason="work_schedule_overlapping_shifts",
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick,
                        reason_code="work_schedule_overlapping_shifts", subject_id=plan.actor_id,
                        reference_id=plan.work_schedule.assignment_id,
                    ),
                )
            next_work_shift_tick, schedule_error = self._next_routine_work_tick(
                state.current_tick,
                first_shift_tick=plan.work_schedule.first_shift_tick,
                cadence_ticks=cadence_ticks,
                shift_duration=work_terms.duration_seconds,
            )
            if schedule_error is not None:
                self._reject_unconsumed_decisions(context)
                return self._empty(
                    state, context, outcome="stopped", reason=schedule_error,
                    interruption=Interruption(
                        kind="boundary", tick=state.current_tick,
                        reason_code=schedule_error, subject_id=plan.actor_id,
                        reference_id=plan.work_schedule.assignment_id,
                    ),
                )
            if (
                next_work_shift_tick is not None
                and (next_work_shift_tick >= plan.target_tick
                     or next_work_shift_tick + work_terms.duration_seconds > plan.target_tick)
            ):
                # There is not enough time to finish the next registered shift.
                # Let the fixed plan deadline close the job without beginning
                # or paying for an incomplete shift.
                next_work_shift_tick = None
            if next_work_shift_tick is not None and state.current_tick < next_work_shift_tick:
                rest_plan = self._routine_work_rest_plan(
                    state, plan.actor_id, next_work_shift_tick,
                )
                if rest_plan is not None:
                    next_work_rest_tick, next_work_rest_duration = rest_plan
            between_work_shifts = (
                next_work_shift_tick is None
                or state.current_tick < next_work_shift_tick
            )
            work_shift_due = (
                next_work_shift_tick is not None
                and state.current_tick == next_work_shift_tick
            )
            if next_work_shift_tick == state.current_tick and work_terms is not None:
                # Keep the ordinary atomic rule when its full interval is quiet.
                # A leased shift can commit attendance and share the clock with
                # independent world events inside the interval.
                shift_end = state.current_tick + work_terms.duration_seconds
                if shift_end > 2**63 - 1:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(
                        state, context, outcome="stopped", reason="work_shift_tick_overflow",
                        interruption=Interruption(
                            kind="rule_required", tick=state.current_tick,
                            reason_code="work_shift_tick_overflow", subject_id=plan.actor_id,
                            reference_id=plan.work_schedule.assignment_id,
                        ),
                    )
                interior = self._next_external_boundary(
                    state, shift_end - 1, actor_id=plan.actor_id,
                    externally_controlled_actor_ids=context.externally_controlled_actor_ids,
                    include_meeting_start=context.allow_autonomous_meeting_attendance,
                )
                interior = self._with_npc_work_boundary(state, shift_end - 1, plan, context, interior)
                if context.work_job_id is not None:
                    interior = self._with_npc_routine_boundary(state, shift_end - 1, plan, context, interior)
                if interior is not None and interior[0] > state.current_tick and context.work_job_id is None:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(
                        state, context, outcome="stopped", reason="work_shift_boundary_required",
                        interruption=Interruption(
                            kind="boundary", tick=interior[0],
                            reason_code="work_shift_boundary_required",
                            subject_id=interior[3], reference_id=interior[2],
                        ),
                    )
                split_work_shift = interior is not None and interior[0] > state.current_tick

        journey = next((row for row in state.travel_journeys
                        if row.actor_id == plan.actor_id and row.status == "in_transit"), None)
        if context.itinerary_auto_care and journey is not None:
            if context.itinerary_journey_id != journey.journey_id or journey.route_id != plan.route_id:
                self._reject_unconsumed_decisions(context)
                reason = "itinerary_auto_care_journey_mismatch"
                return self._empty(
                    state, context, outcome="stopped", reason=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick,
                        reason_code=reason, subject_id=plan.actor_id,
                        reference_id=journey.journey_id,
                    ),
                )
        if journey is not None and plan.activity == "travel" and journey.route_id == plan.route_id:
            return self._advance_travel_journey(state, plan, context, journey)

        if plan.target_tick == state.current_tick:
            may_close = plan.activity in {"work", "study", "wait"} or (
                plan.activity == "rest" and plan.started_tick is not None and plan.started_tick < state.current_tick
            )
            if may_close:
                routine = self._resolve_npc_routine(state, plan, context)
                if routine is not None:
                    return routine
            self._reject_unconsumed_decisions(context)
            if plan.activity == "rest" and plan.started_tick is not None and plan.started_tick < state.current_tick:
                # An annual decision can be resolved at the exact terminal
                # tick after the final rest slice has already committed. That
                # resumed lease may now close normally without starting a
                # second rest interval.
                return self._empty(state, context, outcome="completed", reason="target_reached")
            if plan.activity in {"travel", "produce", "eat", "drink", "rest"}:
                reason = f"{plan.activity}_not_started_before_target"
                return self._empty(
                    state,
                    context,
                    outcome="rule_required",
                    reason=reason,
                    interruption=Interruption(
                        kind="rule_required",
                        tick=state.current_tick,
                        reason_code=reason,
                        subject_id=plan.actor_id,
                    ),
                )
            return self._empty(state, context, outcome="completed", reason="target_reached")

        # A queued explicit move preserves the player's chosen action. The
        # journey still checks departure eligibility and advances through all
        # normal world and survival boundaries.
        if plan.depart_immediately:
            return self._depart_travel_journey(state, plan, context)
        if plan.stationary_only:
            return self._advance_stationary_action(state, plan, context, journey)

        food_resource = self._food_resource(state)
        water_resource = self._water_resource(state)
        food_stock_available = bool(self._food_stock_accounts(state, plan.actor_id, food_resource))
        water_stock_available = bool(self._food_stock_accounts(state, plan.actor_id, water_resource))
        try:
            # The explicit routine schedule owns the due work decision. Ask
            # AgentPolicy only whether critical self-care is needed; ordinary
            # player goals cannot replace an authorized paid shift.
            requested_activity = "wait" if plan.activity == "work" else plan.activity
            decision = self.agent_policy.choose(
                state,
                plan.actor_id,
                requested_activity,
                food_available=food_stock_available,
                water_available=water_stock_available,
            )
        except AmbiguousGoalPriority as conflict:
            request = self._goal_conflict_request(state, context, plan.actor_id, conflict.goal_ids)
            return self._decision_gate(state, context, request, continuation=plan)

        activity = decision.activity
        if activity == "eat" and not food_stock_available:
            activity = "wait"
        if activity == "drink" and not water_stock_available:
            activity = "wait"
        proactive_rest_due = (
            next_work_rest_tick is not None
            and state.current_tick == next_work_rest_tick
            and next_work_rest_duration is not None
        )
        if work_shift_due:
            if activity in {"eat", "drink", "rest"}:
                self._reject_unconsumed_decisions(context)
                reason = "work_shift_self_care_required"
                return self._empty(
                    state, context, outcome="stopped", reason=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick,
                        reason_code=reason, subject_id=plan.actor_id,
                        reference_id=plan.work_schedule.assignment_id if plan.work_schedule else None,
                    ),
                )
            activity = "work"
            if split_work_shift:
                blocked = any(row.assignment_id == plan.work_schedule.assignment_id and (
                    row.work_job_id is None or row.worked_through_tick != row.starts_at_tick
                ) for row in state.npc_attendance)
                if blocked:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(state, context, outcome="stopped", reason="work_slot_already_in_progress")
                attendance = plan_npc_attendance_start(state, plan.work_schedule.assignment_id, work_job_id=context.work_job_id)
                if attendance is None:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(state, context, outcome="stopped", reason="work_shift_unavailable")
                self._reject_unconsumed_decisions(context)
                return self._resolve_npc_work(state, plan, context, NpcWorkTransition("started", attendance))
        elif proactive_rest_due and activity == "wait":
            activity = "rest"
        if activity == "eat" and "do_not_eat" in plan.constraints:
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state,
                context,
                outcome="stopped",
                reason="plan_constraint_do_not_eat",
                interruption=Interruption(kind="stop_condition", tick=state.current_tick, reason_code="plan_constraint_do_not_eat", subject_id=plan.actor_id),
            )
        if activity == "drink" and "do_not_drink" in plan.constraints:
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state,
                context,
                outcome="stopped",
                reason="plan_constraint_do_not_drink",
                interruption=Interruption(kind="stop_condition", tick=state.current_tick, reason_code="plan_constraint_do_not_drink", subject_id=plan.actor_id),
            )
        if activity == "rest" and "do_not_rest" in plan.constraints:
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state,
                context,
                outcome="stopped",
                reason="plan_constraint_do_not_rest",
                interruption=Interruption(kind="stop_condition", tick=state.current_tick, reason_code="plan_constraint_do_not_rest", subject_id=plan.actor_id),
            )

        if activity == "travel" and plan.route_id is not None and callable(
            getattr(self.rule_engine, "evaluate_travel_departure", None)
        ):
            return self._depart_travel_journey(state, plan, context)

        if activity == "work" and plan.work_schedule is not None and work_terms is not None:
            action = (
                WorkShiftIntent(assignment_id=plan.work_schedule.assignment_id),
                work_terms.duration_seconds,
                True,
            )
        elif proactive_rest_due and activity == "rest" and next_work_rest_duration is not None:
            action = (
                RestIntent(duration_ticks=next_work_rest_duration),
                next_work_rest_duration,
                False,
            )
        else:
            action = self._action(state, plan, activity, food_resource, water_resource)
        if isinstance(action, SimulationResolution):
            self._reject_unconsumed_decisions(context)
            return action
        if action is None:
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state,
                context,
                outcome="rule_required",
                reason=f"activity_{activity}_has_no_registered_rule",
                interruption=Interruption(kind="rule_required", tick=state.current_tick, reason_code=f"activity_{activity}_has_no_registered_rule", subject_id=plan.actor_id),
            )
        intent, natural_duration, one_shot = action
        splittable_for_routine = not one_shot and activity in {"wait", "rest"}
        if splittable_for_routine or work_shift_due or activity == "study":
            protected_interval = None
            if next_work_shift_tick is not None and work_terms is not None:
                protected_interval = (next_work_shift_tick, next_work_shift_tick + work_terms.duration_seconds)
            elif activity == "study" and natural_duration is not None:
                # Training awards experience per registered action. A fresh
                # commute must not create a new arrival inside that interval.
                training_end = min(
                    plan.target_tick,
                    state.current_tick + min(natural_duration, context.max_step_ticks, self.policy.max_macro_step_seconds),
                )
                protected_interval = (state.current_tick, training_end)
            routine = self._resolve_npc_routine(state, plan, context, protected_interval=protected_interval)
            if routine is not None:
                return routine

        remaining_plan_ticks = plan.target_tick - state.current_tick
        requested_action_exceeds_window = (
            natural_duration is not None
            and natural_duration > remaining_plan_ticks
            and activity == plan.activity
            and activity in {"eat", "drink"}
        )
        if natural_duration is not None and natural_duration > remaining_plan_ticks:
            if activity in {"eat", "drink"} and not requested_action_exceeds_window:
                # One-shot care cannot be truncated without changing its
                # registered rule. Use the remaining interval as a wait; it
                # still applies the survival rules and cannot cross the target.
                activity = "wait"
                intent = WaitIntent(duration_ticks=remaining_plan_ticks)
                natural_duration = remaining_plan_ticks
                one_shot = False

        horizon = min(
            plan.target_tick,
            state.current_tick + context.max_step_ticks,
            state.current_tick + self.policy.max_macro_step_seconds,
            state.current_tick + 30 * state.calendar.day_seconds,
        )
        external_horizon = horizon
        if natural_duration is not None:
            action_horizon = min(plan.target_tick, state.current_tick + natural_duration)
            if activity == "work" and action_horizon > state.current_tick:
                # WorkShiftIntent owns an atomic registered interval. Future
                # events strictly inside the shift are handled by the preflight
                # above; a boundary exactly at shift completion is resolved in
                # the next window after this one exact settlement.
                external_horizon = action_horizon - 1
            elif activity in {"travel", "produce"} or one_shot:
                external_horizon = action_horizon
            else:
                external_horizon = min(horizon, action_horizon)
        stationary_journey = active_travel_journey(state, plan.actor_id) if activity in {"rest", "eat", "drink", "wait"} else None
        external = self._next_external_boundary(
            state,
            external_horizon,
            actor_id=plan.actor_id,
            externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            deferred_travel_schedule_id=travel_resolution_schedule_id(stationary_journey.journey_id) if stationary_journey else None,
            include_meeting_start=context.allow_autonomous_meeting_attendance
            and activity in {"wait", "study", "rest"} and not one_shot,
        )
        external = self._with_npc_work_boundary(state, external_horizon, plan, context, external)
        autonomous_vacancy_tick = None
        actor_care_tick = None
        if (plan.activity in {"study", "rest", "wait"} or between_work_shifts) and not one_shot:
            candidate = self._next_actor_self_care_boundary(
                state,
                plan.actor_id,
                food_available=food_stock_available,
                water_available=water_stock_available,
            )
            if candidate is not None and state.current_tick < candidate <= external_horizon:
                actor_care_tick = candidate
        if actor_care_tick is not None and actor_care_tick > horizon:
            actor_care_tick = None
        if between_work_shifts and next_work_shift_tick is not None:
            if (
                activity == "wait"
                and next_work_rest_tick is not None
                and state.current_tick < next_work_rest_tick < next_work_shift_tick
                and next_work_rest_tick <= external_horizon
                and (external is None or next_work_rest_tick < external[0])
            ):
                external = (
                    next_work_rest_tick,
                    "routine_work_rest_start",
                    None,
                    plan.actor_id,
                )
            if (
                not one_shot
                and next_work_shift_tick <= external_horizon
                and (external is None or next_work_shift_tick < external[0])
            ):
                external = (
                    next_work_shift_tick,
                    "routine_work_shift_start",
                    None,
                    plan.actor_id,
                )
        if between_work_shifts:
            stop_boundary = self._next_background_stop_boundary(state, plan.stop_conditions)
            if (
                stop_boundary is not None
                and state.current_tick < stop_boundary <= external_horizon
                and (external is None or stop_boundary < external[0])
            ):
                external = (stop_boundary, "stop_condition_boundary", None, plan.actor_id)
        safe_for_vacancy_boundary = (
            context.allow_autonomous_vacancy
            and not one_shot
            and activity in {"wait", "study", "rest"}
            and (
                plan.activity in {"wait", "study", "rest"}
                or plan.activity == "work" and between_work_shifts
            )
        )
        if safe_for_vacancy_boundary:
            from .autonomous_vacancy import next_vacancy_year_boundary

            action_limit = min(horizon, plan.target_tick)
            if natural_duration is not None:
                action_limit = min(action_limit, state.current_tick + natural_duration)
            candidate = next_vacancy_year_boundary(state, action_limit)
            if candidate is not None and (external is None or candidate < external[0]):
                autonomous_vacancy_tick = candidate
                horizon = min(horizon, candidate)
                # Later world/schedule boundaries are re-read after the annual
                # review. Advancing directly to them would cross the exact
                # year tick before the decision gate gets its turn.
                external = None
                if actor_care_tick is not None and actor_care_tick > candidate:
                    actor_care_tick = None
        routine_tick = None
        if splittable_for_routine:
            routine_tick = self._next_npc_routine_tick(state, min(horizon, external_horizon), plan, context)
            if routine_tick is not None and (
                external is not None and external[0] <= routine_tick
                or actor_care_tick is not None and actor_care_tick < routine_tick
            ):
                routine_tick = None
            if routine_tick is not None:
                horizon = routine_tick
                external = None
                if actor_care_tick is not None and actor_care_tick > routine_tick:
                    actor_care_tick = None
        if external is not None and (actor_care_tick is None or external[0] <= actor_care_tick):
            boundary_tick, reason, reference_id, subject_id = external
            if boundary_tick <= state.current_tick:
                self._reject_unconsumed_decisions(context)
                return self._empty(
                    state,
                    context,
                    outcome="rule_required",
                    reason=reason,
                    interruption=Interruption(kind="rule_required", tick=state.current_tick, reason_code=reason, subject_id=subject_id, reference_id=reference_id),
                )
            # Do not run an action over a due system event or a lifetime boundary.
            # Commit clock progress to the exact boundary, then let the worker
            # process it before asking for another action proposal.
            return self._advance_to_boundary(state, plan, context, boundary_tick, reason, reference_id, subject_id, activity)

        if (
            between_work_shifts
            and next_work_shift_tick is not None
            and one_shot
            and natural_duration is not None
            and state.current_tick + natural_duration > next_work_shift_tick
        ):
            # Atomic eat/drink care cannot be cut at a work start. Preserve the
            # authorized time exactly and let the player resolve the urgent
            # need or choose a new start time.
            self._reject_unconsumed_decisions(context)
            reason = "work_shift_self_care_required"
            return self._empty(
                state, context, outcome="stopped", reason=reason,
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick,
                    reason_code=reason, subject_id=plan.actor_id,
                    reference_id=plan.work_schedule.assignment_id if plan.work_schedule else None,
                ),
            )


        if actor_care_tick is not None:
            # Keep the already selected adaptive action active through its first
            # stocked food/water policy boundary. Replanning after that tick lets
            # AgentPolicy select the matching care action without erasing study
            # progress or crossing a competing event horizon.
            action_ticks = actor_care_tick - state.current_tick
            if activity == "rest" and action_ticks < 60:
                activity = "wait"
                intent = WaitIntent(duration_ticks=action_ticks)
            else:
                intent = self._with_duration(intent, action_ticks)
            natural_duration = action_ticks

        if requested_action_exceeds_window:
            self._reject_unconsumed_decisions(context)
            reason = f"{activity}_exceeds_requested_window"
            return self._empty(
                state,
                context,
                outcome="rule_required",
                reason=reason,
                interruption=Interruption(
                    kind="rule_required",
                    tick=state.current_tick,
                    reason_code=reason,
                    subject_id=plan.actor_id,
                ),
            )

        # A route and a recipe are atomic registered actions. They may take longer
        # than the normal macro cap, but may never cross the command target, a due
        # event, or a known lifespan boundary.
        if natural_duration is not None:
            if natural_duration <= 0:
                raise ValueError("Registered action duration must be positive.")
            if activity in {"travel", "produce"}:
                action_end = state.current_tick + natural_duration
                if action_end > plan.target_tick:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(
                        state,
                        context,
                        outcome="rule_required",
                        reason=f"{activity}_exceeds_requested_window",
                        interruption=Interruption(kind="rule_required", tick=state.current_tick, reason_code=f"{activity}_exceeds_requested_window", subject_id=plan.actor_id),
                    )
            elif not one_shot and natural_duration > horizon - state.current_tick:
                available_ticks = horizon - state.current_tick
                if available_ticks <= 0:
                    return self._empty(state, context, outcome="completed", reason="target_reached")
                if activity == "rest" and available_ticks < 60:
                    slice_end = state.current_tick + available_ticks
                    routine_split = routine_tick == slice_end and plan.target_tick - state.current_tick >= 60
                    if (
                        activity == plan.activity and autonomous_vacancy_tick != slice_end and not routine_split
                        and not self._recent_hosted_trip_during_rest(state, plan, context)
                    ):
                        self._reject_unconsumed_decisions(context)
                        reason = "rest_below_minimum_duration"
                        return self._empty(
                            state,
                            context,
                            outcome="rule_required",
                            reason=reason,
                            interruption=Interruption(
                                kind="rule_required",
                                tick=state.current_tick,
                                reason_code=reason,
                                subject_id=plan.actor_id,
                            ),
                        )
                    # RestIntent has a 60-second minimum. A shorter incidental
                    # rest slice becomes a bounded wait, preserving survival
                    # checks and the original plan target. The same conversion
                    # handles a sub-minute governance or routine boundary without
                    # discarding the already-authorized rest window.
                    activity = "wait"
                    intent = WaitIntent(duration_ticks=available_ticks)
                    natural_duration = available_ticks
                else:
                    natural_duration = available_ticks
                    intent = self._with_duration(intent, natural_duration)

        action_end = state.current_tick + (natural_duration or 0)
        for condition in plan.stop_conditions:
            stop_boundary = self._stop_boundary(state, condition, activity, plan, natural_duration)
            if stop_boundary is not None and stop_boundary < action_end:
                if stop_boundary <= state.current_tick:
                    return self._empty(
                        state,
                        context,
                        outcome="stopped",
                        reason="stop_condition_already_satisfied",
                        interruption=Interruption(kind="stop_condition", tick=state.current_tick, reason_code="stop_condition_already_satisfied", subject_id=condition.subject_id),
                    )
                intent = self._with_duration(intent, stop_boundary - state.current_tick)
                natural_duration = stop_boundary - state.current_tick
                action_end = stop_boundary

        self._reject_unconsumed_decisions(context)
        slice_command_id = uuid5(
            context.command_id,
            f"{context.world_seed}:simulation:{context.state_version}:{state.current_tick}:{activity}:{intent.kind}",
        )
        rule_context = RuleContext(
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=slice_command_id,
            actor_id=plan.actor_id,
            ruleset_version=context.ruleset_version or RULES_VERSION,
            simulation_command_id=context.command_id,
            continuous_training=plan.activity == "study" and activity == "study",
        )
        is_paid_work_shift = plan.activity == "work" and activity == "work"
        if is_paid_work_shift:
            # The repository validates a paid work batch by replaying the
            # canonical action.work_shift command. Keep its command identity
            # and event list exactly equal to one ordinary RuleEngine call.
            rule_context = rule_context.model_copy(update={"command_id": context.command_id})
            resolution = self.rule_engine.evaluate(state, intent, rule_context)
        else:
            background_choices = self.agent_policy.choose_background(
                state,
                food_resource_id=food_resource,
                exclude_actor_ids=frozenset({plan.actor_id}),
            )
            world_action = getattr(self.rule_engine, "evaluate_world_action", None)
            resolution = (
                world_action(state, intent, rule_context, background_choices)
                if callable(world_action)
                else self.rule_engine.evaluate(state, intent, rule_context)
            )
        self._validate_rule_batch(state, resolution)
        to_tick = state.current_tick + resolution.elapsed_ticks
        if to_tick > plan.target_tick:
            raise RuntimeError("RuleEngine returned an event batch beyond the requested target tick.")
        if natural_duration is not None and to_tick > state.current_tick + natural_duration:
            raise RuntimeError("RuleEngine elapsed time exceeds the registered activity duration.")
        if resolution.outcome.code == "lifespan_expired":
            expired_ids = {
                event.entity_id for event in resolution.events if isinstance(event, LifeEndedEvent)
            }
            actor_expired = plan.actor_id in expired_ids
            return self._resolved(
                state,
                context,
                resolution,
                outcome="stopped" if actor_expired else "advanced",
                boundary="actor_lifespan_expired" if actor_expired else "npc_lifespan_expired",
                interruption=(
                    Interruption(kind="boundary", tick=to_tick, reason_code="actor_lifespan_expired", subject_id=plan.actor_id)
                    if actor_expired else None
                ),
                continuation=None if actor_expired else self._continuation(plan, to_tick),
            )
        if resolution.outcome.code in {"survival_pressure", "died_of_deprivation"}:
            return self._resolved(
                state,
                context,
                resolution,
                outcome="stopped",
                boundary=resolution.outcome.code,
                interruption=Interruption(kind="rule_required", tick=to_tick, reason_code=resolution.outcome.code, subject_id=plan.actor_id),
            )
        for condition in plan.stop_conditions:
            if self._stop_satisfied_after(state, resolution.events, condition):
                interruption = Interruption(kind="stop_condition", tick=to_tick, reason_code="stop_condition_reached", subject_id=condition.subject_id)
                return self._resolved(
                    state,
                    context,
                    resolution,
                    outcome="stopped",
                    boundary="stop_condition",
                    interruption=interruption,
                    continuation=None,
                )

        if resolution.interruptions:
            first = resolution.interruptions[0]
            interruption = Interruption(
                kind="boundary",
                tick=first.at_tick if first.at_tick is not None else to_tick,
                reason_code=first.code,
                subject_id=plan.actor_id,
            )
            return self._resolved(state, context, resolution, outcome="stopped", boundary=first.code, interruption=interruption)

        if resolution.outcome.status in {"failed", "rejected"}:
            if is_paid_work_shift:
                reason = f"work_shift_{resolution.outcome.code}"
                return self._resolved(
                    state, context, resolution, outcome="stopped", boundary=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=to_tick, reason_code=reason,
                        subject_id=plan.actor_id,
                        reference_id=plan.work_schedule.assignment_id if plan.work_schedule else None,
                    ),
                )
            return self._resolved(
                state,
                context,
                resolution,
                outcome="stopped",
                boundary=resolution.outcome.code,
                interruption=Interruption(kind="rule_required", tick=to_tick, reason_code=resolution.outcome.code, subject_id=plan.actor_id),
            )

        if is_paid_work_shift:
            paid_event = next((event for event in resolution.events if isinstance(event, WorkShiftPaidEvent)), None)
            if paid_event is None:
                reason = "work_shift_not_paid"
                return self._resolved(
                    state, context, resolution, outcome="stopped", boundary=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=to_tick, reason_code=reason,
                        subject_id=plan.actor_id,
                        reference_id=plan.work_schedule.assignment_id if plan.work_schedule else None,
                    ),
                )
            balance_target = plan.work_schedule.balance_target if plan.work_schedule is not None else None
            worker_balance_after = paid_event.plan.ledger_entries[0].balance_after
            pending_at_completion = self._next_external_boundary(
                state, to_tick, actor_id=plan.actor_id,
                externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            )
            pending_at_completion = self._with_npc_work_boundary(state, to_tick, plan, context, pending_at_completion)
            vacancy_due_after_shift = False
            if context.allow_autonomous_vacancy:
                from .autonomous_vacancy import next_vacancy_year_boundary

                vacancy_tick = next_vacancy_year_boundary(state, to_tick)
                vacancy_due_after_shift = vacancy_tick is not None and vacancy_tick <= to_tick
            same_tick_boundary_pending = (
                pending_at_completion is not None
                and pending_at_completion[0] == to_tick
            ) or vacancy_due_after_shift
            settled_state = None
            if not same_tick_boundary_pending and self._npc_routine_hosted(context) and any(
                "routine_mobility" in entity.components for entity in state.entities.values()
            ):
                settled_state = state
                for event in resolution.events:
                    settled_state = apply_event(settled_state, event)
                same_tick_boundary_pending = npc_routine_candidate(
                    settled_state, excluded_actor_ids=self._npc_routine_exclusions(plan, context),
                ) is not None
            if (
                not same_tick_boundary_pending
                and balance_target is not None
                and worker_balance_after >= balance_target
            ):
                return self._resolved(
                    state, context, resolution, outcome="completed",
                    boundary="work_balance_target_reached", interruption=None,
                )
            purchase_target = plan.work_schedule.purchase_target if plan.work_schedule is not None else None
            if not same_tick_boundary_pending and purchase_target is not None:
                if settled_state is None:
                    settled_state = state
                    for event in resolution.events:
                        settled_state = apply_event(settled_state, event)
                try:
                    purchase_quote = self._work_purchase_quote(
                        settled_state, purchase_target, actor_id=plan.actor_id,
                        workplace_id=work_location_id,
                        wage_resource_id=settled_state.resources[work_account_id].resource_id,
                    )
                except ValueError:
                    return self._resolved(
                        state, context, resolution, outcome="stopped",
                        boundary="work_purchase_target_unavailable",
                        interruption=Interruption(
                            kind="rule_required", tick=to_tick,
                            reason_code="work_purchase_target_unavailable", subject_id=plan.actor_id,
                            reference_id=purchase_target.listing_id,
                        ),
                    )
                if settled_state.resources[work_account_id].balance >= purchase_quote:
                    return self._resolved(
                        state, context, resolution, outcome="completed",
                        boundary="work_purchase_target_affordable", interruption=None,
                        work_purchase_affordability=WorkPurchaseAffordabilityReceipt(
                            listing_id=purchase_target.listing_id,
                            quantity=purchase_target.quantity,
                            currency_resource_id=purchase_target.currency_resource_id,
                            total_price_minor=purchase_quote,
                            wage_balance=settled_state.resources[work_account_id].balance,
                            confirmed_tick=settled_state.current_tick,
                        ),
                    )
            if not same_tick_boundary_pending and to_tick >= plan.target_tick:
                return self._resolved(
                    state, context, resolution, outcome="completed",
                    boundary="target_reached", interruption=None,
                )
            return self._resolved(
                state, context, resolution, outcome="advanced",
                boundary=(
                    "work_shift_complete_pending_boundary"
                    if same_tick_boundary_pending else "work_shift_complete"
                ),
                interruption=None,
                continuation=self._continuation(plan, to_tick),
            )

        actor_policy_boundary_reached = actor_care_tick is not None and to_tick == actor_care_tick

        if plan.activity in {"travel", "produce"} and activity != plan.activity:
            if to_tick >= plan.target_tick:
                reason = f"{plan.activity}_not_started_before_target"
                return self._resolved(
                    state,
                    context,
                    resolution,
                    outcome="rule_required",
                    boundary=reason,
                    interruption=Interruption(
                        kind="rule_required",
                        tick=to_tick,
                        reason_code=reason,
                        subject_id=plan.actor_id,
                    ),
                )
            return self._resolved(
                state,
                context,
                resolution,
                outcome="advanced",
                boundary="incidental_care_before_atomic_action",
                interruption=Interruption(
                    kind="boundary",
                    tick=to_tick,
                    reason_code="incidental_care_completed",
                    subject_id=plan.actor_id,
                ),
                continuation=self._continuation(plan, to_tick),
            )

        requested_one_shot_completed = one_shot and activity == plan.activity
        target_reached = to_tick >= plan.target_tick
        if autonomous_vacancy_tick is not None and to_tick == autonomous_vacancy_tick:
            return self._resolved(
                state,
                context,
                resolution,
                outcome="advanced",
                boundary="autonomous_vacancy_year_boundary",
                interruption=None,
                continuation=self._continuation(plan, to_tick),
            )
        if routine_tick is not None and to_tick == routine_tick:
            return self._resolved(
                state, context, resolution, outcome="advanced", boundary="npc_routine_boundary",
                interruption=None, continuation=self._continuation(plan, state.current_tick),
            )
        if activity == "study" and target_reached and self._npc_routine_hosted(context) and any(
            "routine_mobility" in entity.components for entity in state.entities.values()
        ):
            studied_state = state
            for event in resolution.events:
                studied_state = apply_event(studied_state, event)
            if npc_routine_candidate(studied_state, excluded_actor_ids=self._npc_routine_exclusions(plan, context)) is not None:
                return self._resolved(
                    state, context, resolution, outcome="advanced", boundary="npc_routine_boundary",
                    interruption=None, continuation=self._continuation(plan, state.current_tick),
                )
        if requested_one_shot_completed or target_reached:
            outcome = "completed"
            continuation = None
            interruption = None
        else:
            outcome = "advanced"
            continuation = plan.model_copy(update={"started_tick": state.current_tick if plan.started_tick is None else plan.started_tick})
            interruption = Interruption(
                kind="boundary", tick=to_tick,
                reason_code=(
                    "actor_self_care_threshold" if actor_policy_boundary_reached
                    else "incidental_care_completed" if one_shot
                    else "action_step_complete"
                ),
                subject_id=plan.actor_id,
            )
        boundary = (
            f"{activity}_complete" if requested_one_shot_completed
            else "target_reached" if target_reached
            else "actor_self_care_threshold" if actor_policy_boundary_reached
            else "incidental_care_completed" if one_shot
            else "adaptive_step"
        )
        return self._resolved(
            state,
            context,
            resolution,
            outcome=outcome,
            boundary=boundary,
            interruption=interruption,
            continuation=continuation,
        )

    def _execute_exact_consumption(self, state, plan, context) -> SimulationResolution:
        """Revalidate the requested stock and amount after same-tick work has settled."""
        self._reject_unconsumed_decisions(context)
        intent = plan.consumption
        if state.current_tick != plan.started_tick or intent is None:
            return self._empty(state, context, outcome="rule_required", reason="consumption_start_changed")
        rule_context = RuleContext(
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            actor_id=plan.actor_id, ruleset_version=context.ruleset_version or RULES_VERSION,
        )
        choices = self.agent_policy.choose_background(
            state, exclude_actor_ids=frozenset({plan.actor_id}),
        )
        world_action = getattr(self.rule_engine, "evaluate_world_action", None)
        resolution = (
            world_action(state, intent, rule_context, choices) if callable(world_action)
            else self.rule_engine.evaluate(state, intent, rule_context)
        )
        self._validate_rule_batch(state, resolution)
        if resolution.elapsed_ticks == intent.duration_ticks:
            return self._resolved(state, context, resolution, outcome="completed",
                                  boundary=resolution.outcome.code, interruption=None)
        if resolution.elapsed_ticks != 0:
            raise RuntimeError("Exact consumption must run for its complete registered duration.")
        return self._resolved(
            state, context, resolution, outcome="rule_required", boundary=resolution.outcome.code,
            interruption=Interruption(kind="rule_required", tick=state.current_tick,
                                      reason_code=resolution.outcome.code, subject_id=plan.actor_id),
        )

    def _depart_travel_journey(self, state, plan, context) -> SimulationResolution:
        self._reject_unconsumed_decisions(context)
        rule_context = RuleContext(
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            actor_id=plan.actor_id, ruleset_version=context.ruleset_version,
        )
        if context.itinerary_journey_id is None:
            resolution = self.rule_engine.evaluate_travel_departure(state, plan.route_id, rule_context)
        else:
            resolution = self.rule_engine.evaluate_travel_departure(
                state, plan.route_id, rule_context,
                itinerary_journey_id=context.itinerary_journey_id,
            )
        departure = next((event for event in resolution.events if isinstance(event, TravelJourneyChangedEvent)), None)
        if departure is not None and departure.journey.arrival_tick <= plan.target_tick:
            self._validate_rule_batch(state, resolution)
            return self._resolved(
                state, context, resolution, outcome="advanced", boundary="travel_departed",
                interruption=None, continuation=self._continuation(plan, state.current_tick),
            )
        reason = "travel_exceeds_requested_window" if departure is not None else resolution.outcome.code
        return self._empty(
            state, context, outcome="rule_required", reason=reason,
            interruption=Interruption(kind="rule_required", tick=state.current_tick, reason_code=reason, subject_id=plan.actor_id),
        )

    def _advance_travel_journey(self, state, plan, context, journey) -> SimulationResolution:
        if context.itinerary_auto_care:
            if context.itinerary_journey_id != journey.journey_id:
                self._reject_unconsumed_decisions(context)
                reason = "itinerary_auto_care_journey_mismatch"
                return self._empty(
                    state, context, outcome="stopped", reason=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick,
                        reason_code=reason, subject_id=plan.actor_id,
                        reference_id=journey.journey_id,
                    ),
                )
            return self._advance_travel_journey_with_itinerary_care(state, plan, context, journey)

        due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
        horizon = min(plan.target_tick, due, state.current_tick + context.max_step_ticks,
                      state.current_tick + self.policy.max_macro_step_seconds)
        if context.allow_autonomous_vacancy:
            from .autonomous_vacancy import next_vacancy_year_boundary

            review_tick = next_vacancy_year_boundary(state, horizon)
            if review_tick is not None:
                horizon = min(horizon, review_tick)
        if horizon <= state.current_tick:
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state, context, outcome="rule_required", reason="travel_arrival_after_target",
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick, reason_code="travel_arrival_after_target",
                    subject_id=journey.actor_id,
                ),
            )
        routine = self._resolve_npc_routine(state, plan, context)
        if routine is not None:
            return routine
        external = self._next_external_boundary(
            state, horizon, actor_id=plan.actor_id,
            externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            include_meeting_start=context.allow_autonomous_meeting_attendance,
        )
        external = self._with_npc_routine_boundary(state, horizon, plan, context, external)
        if external is not None:
            boundary, reason, reference, subject = external
            return self._advance_to_boundary(state, plan, context, boundary, reason, reference, subject, "travel")
        return self._advance_to_boundary(state, plan, context, horizon, "travel_in_progress", None, journey.actor_id, "travel")

    def _advance_travel_journey_with_itinerary_care(self, state, plan, context, journey):
        """Advance a verified itinerary leg, resolving opted-in stock care in transit."""

        if journey.actor_id != plan.actor_id or journey.route_id != plan.route_id:
            self._reject_unconsumed_decisions(context)
            reason = "itinerary_auto_care_journey_mismatch"
            return self._empty(
                state, context, outcome="stopped", reason=reason,
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick,
                    reason_code=reason, subject_id=plan.actor_id,
                    reference_id=journey.journey_id,
                ),
            )
        if journey.arrival_tick > plan.target_tick:
            return self._itinerary_auto_care_stop(
                state, plan, context, journey, "itinerary_auto_care_deadline",
                kind="boundary",
            )

        due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
        horizon = min(
            plan.target_tick,
            due,
            state.current_tick + context.max_step_ticks,
            state.current_tick + self.policy.max_macro_step_seconds,
        )
        if horizon <= state.current_tick:
            return self._itinerary_auto_care_stop(
                state, plan, context, journey, "itinerary_auto_care_deadline",
                kind="boundary",
            )

        needs = self._actor_needs(state, plan.actor_id)
        care = self._itinerary_care_candidates(state, plan.actor_id, needs)
        due_care = [item for item in care if item[0] <= state.current_tick]
        if due_care:
            # Match AgentPolicy's deterministic priority when multiple needs
            # reach their threshold on the same world tick: water, food, rest.
            _tick, _priority, need_name, resource_id, available = min(
                due_care, key=lambda item: (item[0], -item[1], item[2]),
            )
            if need_name in {"food", "water"}:
                if resource_id is None or available < Decimal("0.1"):
                    return self._itinerary_auto_care_stop(
                        state, plan, context, journey,
                        "itinerary_auto_care_resource_unavailable", kind="rule_required",
                    )
                intent = ConsumeIntent(
                    need=need_name,
                    resource_id=resource_id,
                    quantity=min(Decimal("1"), available),
                )
                return self._resolve_itinerary_auto_care(
                    state, plan, context, journey, intent, intent.duration_ticks,
                )

            duration = self._itinerary_rest_duration(state, needs)
            if duration is None:
                return self._itinerary_auto_care_stop(
                    state, plan, context, journey, "itinerary_auto_care_unsafe",
                    kind="boundary",
                )
            # RuleEngine's ordinary stationary-action guard will not cross a
            # still-queued route hazard/arrival tick. End this sleep slice at
            # that tick so the canonical delay event can move it by exactly
            # the stationary time actually committed.
            until_journey_boundary = due - state.current_tick
            if until_journey_boundary < 60:
                return self._advance_to_boundary(
                    state, plan, context, due, "travel_in_progress", None,
                    journey.actor_id, "travel",
                )
            duration = min(duration, until_journey_boundary)
            return self._resolve_itinerary_auto_care(
                state, plan, context, journey, RestIntent(duration_ticks=duration), duration,
            )

        routine = self._resolve_npc_routine(state, plan, context)
        if routine is not None:
            return routine
        # Atomic food and water care above completes on its registered tick.
        # Once care is current again, ordinary in-transit progress stops at
        # the annual review tick so the same lease can resolve governance
        # before the journey advances farther.
        if context.allow_autonomous_vacancy:
            from .autonomous_vacancy import next_vacancy_year_boundary

            review_tick = next_vacancy_year_boundary(state, horizon)
            if review_tick is not None:
                horizon = min(horizon, review_tick)

        care_tick = min((item[0] for item in care), default=None)
        external = self._next_external_boundary(
            state, horizon, actor_id=plan.actor_id,
            externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            include_meeting_start=context.allow_autonomous_meeting_attendance,
        )
        external = self._with_npc_routine_boundary(state, horizon, plan, context, external)
        if external is not None and (care_tick is None or external[0] <= care_tick):
            boundary, reason, reference, subject = external
            if boundary <= state.current_tick:
                return self._empty(
                    state, context, outcome="rule_required", reason=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick,
                        reason_code=reason, subject_id=subject, reference_id=reference,
                    ),
                )
            return self._advance_to_boundary(
                state, plan, context, boundary, reason, reference, subject, "travel",
            )
        if care_tick is not None and state.current_tick < care_tick <= horizon:
            return self._advance_to_boundary(
                state, plan, context, care_tick, "itinerary_auto_care_threshold",
                None, plan.actor_id, "travel",
            )
        return self._advance_to_boundary(
            state, plan, context, horizon, "travel_in_progress", None, journey.actor_id, "travel",
        )

    def _itinerary_care_candidates(self, state, actor_id: UUID, needs: NeedsComponent):
        """Return exact future care thresholds and canonical owned stock per need."""

        food_resource = self._food_resource(state)
        water_resource = self._water_resource(state)
        food_threshold = int(getattr(self.agent_policy, "eat_when_below", 25))
        rest_threshold = int(getattr(self.agent_policy, "rest_when_below", 30))
        candidates = []
        for name, priority, rate, resource_id in (
            ("water", 120, PASSIVE_NEED_RATES_PER_DAY["water"], water_resource),
            ("food", 100, PASSIVE_NEED_RATES_PER_DAY["food"], food_resource),
        ):
            need = getattr(needs, name)
            if need is None:
                continue
            threshold = min(food_threshold, need.critical_below)
            tick = next_need_threshold_tick(
                need, threshold=threshold, rate_per_day=rate,
                from_tick=state.current_tick, day_seconds=state.calendar.day_seconds,
            )
            available = (
                self._owned_inventory_quantity(state, actor_id, resource_id, at_tick=tick)
                if resource_id is not None else Decimal("0")
            )
            candidates.append((tick, priority, name, resource_id, available))
        if needs.rest is not None:
            tick = next_need_threshold_tick(
                needs.rest,
                threshold=min(99, rest_threshold),
                rate_per_day=PASSIVE_NEED_RATES_PER_DAY["rest"],
                from_tick=state.current_tick,
                day_seconds=state.calendar.day_seconds,
            )
            candidates.append((tick, 80, "rest", None, Decimal("0")))
        return tuple(sorted(candidates, key=lambda item: (item[0], -item[1], item[2])))

    def _itinerary_rest_duration(self, state, needs: NeedsComponent) -> int | None:
        need = needs.rest
        if need is None:
            return None
        threshold = min(99, int(getattr(self.agent_policy, "rest_when_below", 30)))
        day_seconds = state.calendar.day_seconds
        if need.value > threshold:
            return 60

        # Aim for twelve points of recovery (eight hours at the registered
        # rest rate), but retain the existing minimum needed to get above the
        # policy threshold. A low starting value may need another bounded
        # sleep window; one automatic window never exceeds eight game hours.
        target_value = min(100, max(threshold + 1, need.value + 12))
        required_numerator = (target_value - need.value) * day_seconds + need.decay_remainder
        target_duration = max(60, (required_numerator + 35) // 36)
        return min(max(60, day_seconds // 3), target_duration)

    @staticmethod
    def _actor_needs(state, actor_id: UUID) -> NeedsComponent:
        actor = state.entities.get(actor_id)
        raw_needs = actor.components.get("needs") if actor is not None else None
        return NeedsComponent.model_validate(raw_needs) if raw_needs is not None else NeedsComponent()

    def _resolve_itinerary_auto_care(self, state, plan, context, journey, intent, duration):
        slack = plan.target_tick - journey.arrival_tick
        is_rest = isinstance(intent, RestIntent)
        if slack < 0:
            return self._itinerary_auto_care_stop(
                state, plan, context, journey, "itinerary_auto_care_deadline",
                kind="boundary",
            )
        # Rest is a splittable window but consumption remains one exact rule
        # action. Search only as far as the authorized deadline, and apply any
        # shortening after the first competing boundary has been selected.
        if is_rest:
            action_window = min(
                duration, slack, context.max_step_ticks, self.policy.max_macro_step_seconds,
            )
        elif duration > slack:
            # Inspect a due-now world boundary first, but never advance toward
            # a future boundary when an atomic care action cannot fit.
            action_window = 0
        else:
            action_window = duration
        action_end = state.current_tick + action_window
        external = self._next_external_boundary(
            state,
            max(state.current_tick, action_end - 1),
            actor_id=plan.actor_id,
            externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            deferred_travel_schedule_id=travel_resolution_schedule_id(journey.journey_id),
            include_meeting_start=context.allow_autonomous_meeting_attendance and is_rest,
        )
        care_boundaries = [
            item[0] for item in self._itinerary_care_candidates(
                state, plan.actor_id, self._actor_needs(state, plan.actor_id),
            )
            if state.current_tick < item[0] < action_end
        ]
        care_boundary = min(care_boundaries, default=None)
        boundaries = []
        if external is not None:
            boundaries.append((external[0], 0, external[1], external[2], external[3]))
        if care_boundary is not None:
            # World events win same-tick ties with an ordinary care threshold.
            boundaries.append((care_boundary, 1, "itinerary_auto_care_threshold", None, plan.actor_id))
        if is_rest and context.allow_autonomous_vacancy:
            from .autonomous_vacancy import next_vacancy_year_boundary

            review_tick = next_vacancy_year_boundary(state, action_end)
            if review_tick is not None:
                boundaries.append((review_tick, 2, "autonomous_vacancy_year_boundary", None, None))
        first_boundary = min(boundaries, default=None, key=lambda item: (item[0], item[1]))
        if first_boundary is not None:
            boundary, _priority, reason, reference, subject = first_boundary
            if boundary <= state.current_tick:
                return self._empty(
                    state, context, outcome="rule_required", reason=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick,
                        reason_code=reason, subject_id=subject, reference_id=reference,
                    ),
                )

        if not is_rest and duration > slack:
            return self._itinerary_auto_care_stop(
                state, plan, context, journey, "itinerary_auto_care_deadline",
                kind="boundary",
            )

        if is_rest:
            if action_window < 60:
                return self._itinerary_auto_care_stop(
                    state, plan, context, journey, "itinerary_auto_care_deadline",
                    kind="boundary",
                )
            if first_boundary is not None and first_boundary[0] < action_end:
                boundary, _priority, reason, reference, subject = first_boundary
                duration = boundary - state.current_tick
                if duration < 60:
                    return self._advance_to_boundary(
                        state, plan, context, boundary, reason, reference, subject, "travel",
                    )
                intent = RestIntent(duration_ticks=duration)
            else:
                duration = action_window
                intent = RestIntent(duration_ticks=duration)
        else:
            if first_boundary is not None and first_boundary[0] < action_end:
                boundary, _priority, reason, reference, subject = first_boundary
                return self._advance_to_boundary(
                    state, plan, context, boundary, reason, reference, subject, "travel",
                )
            if duration > slack:
                return self._itinerary_auto_care_stop(
                    state, plan, context, journey, "itinerary_auto_care_deadline",
                    kind="boundary",
                )

        self._reject_unconsumed_decisions(context)
        rule_context = RuleContext(
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=plan.actor_id,
            ruleset_version=context.ruleset_version,
            simulation_command_id=context.command_id,
        )
        care_receipt = None
        if type(self.rule_engine) is RuleEngine:
            resolution, care_receipt = evaluate_itinerary_care(state, intent, rule_context)
        else:
            resolution = self.rule_engine.evaluate(state, intent, rule_context)
        if resolution.elapsed_ticks != duration or resolution.outcome.status != "succeeded":
            if is_rest and resolution.elapsed_ticks > 0:
                # A life/deprivation boundary that coincides with the end of a
                # sleep slice belongs to normal world-boundary resolution. Do
                # not turn a partial RestIntent into an itinerary-care pause.
                boundary = self._next_external_boundary(
                    state,
                    state.current_tick + max(duration, resolution.elapsed_ticks),
                    actor_id=plan.actor_id,
                    externally_controlled_actor_ids=context.externally_controlled_actor_ids,
                    deferred_travel_schedule_id=travel_resolution_schedule_id(journey.journey_id),
                )
                if boundary is not None and boundary[0] > state.current_tick:
                    tick, reason, reference, subject = boundary
                else:
                    tick = state.current_tick + resolution.elapsed_ticks
                    reason, reference, subject = resolution.outcome.code, None, plan.actor_id
                return self._advance_to_boundary(
                    state, plan, context, tick, reason, reference, subject, "travel",
                )
            reason = (
                "itinerary_auto_care_resource_unavailable"
                if resolution.outcome.status == "rejected"
                else "itinerary_auto_care_unsafe"
            )
            return self._itinerary_auto_care_stop(
                state, plan, context, journey, reason,
                kind="rule_required" if reason.endswith("unavailable") else "boundary",
            )
        self._validate_rule_batch(state, resolution)
        unsafe = any(
            isinstance(event, LifeEndedEvent) for event in resolution.events
        )
        reason = "itinerary_auto_care_unsafe" if unsafe else "itinerary_auto_care_completed"
        simulation_resolution = self._resolved(
            state, context, resolution,
            outcome="stopped" if unsafe else "advanced",
            boundary=reason,
            interruption=(
                Interruption(
                    kind="boundary", tick=state.current_tick + resolution.elapsed_ticks,
                    reason_code=reason, subject_id=plan.actor_id,
                    reference_id=journey.journey_id,
                ) if unsafe else None
            ),
            continuation=None if unsafe else self._continuation(plan, state.current_tick),
        )
        if care_receipt is not None and not unsafe:
            simulation_resolution._itinerary_care_receipt = care_receipt
        return simulation_resolution

    def _itinerary_auto_care_stop(self, state, plan, context, journey, reason, *, kind):
        self._reject_unconsumed_decisions(context)
        return self._empty(
            state, context, outcome="stopped", reason=reason,
            interruption=Interruption(
                kind=kind, tick=state.current_tick, reason_code=reason,
                subject_id=plan.actor_id, reference_id=journey.journey_id,
            ),
        )

    def _plan_world_advance(
        self,
        state: WorldState,
        plan: WorldAdvancePlan,
        context: SimulationContext,
    ) -> SimulationResolution:
        """Advance every resident under ordinary policy without choosing a focal actor."""
        if plan.started_tick is not None and plan.started_tick > state.current_tick:
            raise ValueError("World-advance started_tick cannot be in the future.")
        if plan.target_tick < state.current_tick:
            raise ValueError("World-advance target_tick cannot precede the current world tick.")

        due_now = self._next_external_boundary(
            state, state.current_tick, externally_controlled_actor_ids=context.externally_controlled_actor_ids,
        )
        if due_now is not None:
            _tick, reason, reference_id, subject_id = due_now
            if reason == "npc_plan_due":
                return self._resolve_npc_plan_execution(state, plan, context, reference_id)
            if reason == "background_deprivation_pending":
                self._reject_unconsumed_decisions(context)
                return self._resolve_background_deprivation(state, plan, context, subject_id)
            scheduled = next((event for event in state.scheduled_events if event.id == reference_id), None) if reference_id else None
            if scheduled is not None:
                if scheduled.due_tick < state.current_tick:
                    self._reject_unconsumed_decisions(context)
                    return self._empty(
                        state,
                        context,
                        outcome="rule_required",
                        reason="scheduled_event_overdue_reconciliation_required",
                        interruption=Interruption(
                            kind="rule_required", tick=state.current_tick,
                            reason_code="scheduled_event_overdue_reconciliation_required",
                            reference_id=scheduled.id,
                        ),
                    )
                if scheduled.phase == "decision":
                    return self._decision_gate(
                        state, context, self._scheduled_decision_request(state, context, scheduled),
                        continuation=plan,
                    )
                return self._resolve_scheduled_event(state, scheduled, context, plan)
            return self._resolve_current_lifespan_boundary(state, context, subject_id, plan)

        succession_request = self._pending_succession_request(state, context)
        if succession_request is not None:
            return self._decision_gate(state, context, succession_request, continuation=plan)
        vacancy_resolution = self.resolve_autonomous_vacancy(state, plan, context)
        if vacancy_resolution is not None:
            return vacancy_resolution

        has_work = bool(state.npc_attendance) or any(row.ended_tick is None for row in state.work_assignments)
        work = npc_work_transition(state, excluded_actor_ids=context.externally_controlled_actor_ids) if has_work else None
        if work is not None:
            self._reject_unconsumed_decisions(context)
            return self._resolve_npc_work(state, plan, context, work)
        meeting_resolution = self.resolve_autonomous_meeting_attendance(state, plan, context)
        if meeting_resolution is not None:
            return meeting_resolution

        if self._stop_satisfied(state, plan.stop_conditions):
            self._reject_unconsumed_decisions(context)
            return self._empty(
                state, context, outcome="stopped", reason="stop_condition_already_satisfied",
                interruption=Interruption(
                    kind="stop_condition", tick=state.current_tick,
                    reason_code="stop_condition_already_satisfied",
                ),
            )
        routine = self._resolve_npc_routine(state, plan, context)
        if routine is not None:
            return routine
        if plan.target_tick == state.current_tick:
            self._reject_unconsumed_decisions(context)
            return self._empty(state, context, outcome="completed", reason="target_reached")

        steady_macro_days = context.max_steady_macro_days
        if steady_macro_days is None:
            # Jobs persisted before the depth profile cap retain the original
            # one-month steady-state horizon.
            steady_macro_days = 30
        horizon = min(
            plan.target_tick,
            state.current_tick + context.max_step_ticks,
            state.current_tick + self.policy.max_macro_step_seconds,
            # Background routine aggregation is bounded by the selected depth
            # profile. Each horizon remains an auditable checkpoint before
            # another harvest/deprivation/lifespan boundary is planned.
            state.current_tick + steady_macro_days * state.calendar.day_seconds,
        )
        external = self._next_external_boundary(
            state, horizon, externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            include_meeting_start=context.allow_autonomous_meeting_attendance,
        )
        if context.allow_autonomous_vacancy:
            from .autonomous_vacancy import next_vacancy_year_boundary

            review_tick = next_vacancy_year_boundary(state, horizon)
            if review_tick is not None and (external is None or review_tick < external[0]):
                external = (review_tick, "autonomous_vacancy_year_boundary", None, None)
        reason = "world_advance_window"
        reference_id = subject_id = None
        to_tick = horizon
        if external is not None:
            to_tick, reason, reference_id, subject_id = external
            if to_tick <= state.current_tick:
                self._reject_unconsumed_decisions(context)
                return self._empty(
                    state, context, outcome="rule_required", reason=reason,
                    interruption=Interruption(
                        kind="rule_required", tick=state.current_tick, reason_code=reason,
                        subject_id=subject_id, reference_id=reference_id,
                    ),
                )

        work_tick = (
            next_npc_work_tick(state, to_tick, excluded_actor_ids=context.externally_controlled_actor_ids)
            if has_work else None
        )
        if work_tick is not None and (external is None or work_tick < to_tick):
            to_tick, reason = work_tick, "npc_work_boundary"
            reference_id = subject_id = None

        routine_tick = self._next_npc_routine_tick(state, to_tick, plan, context)
        if routine_tick is not None and (routine_tick < to_tick or reason == "world_advance_window"):
            to_tick, reason = routine_tick, "npc_routine_boundary"
            reference_id = subject_id = None

        stop_boundary = self._next_background_stop_boundary(state, plan.stop_conditions)
        if stop_boundary is not None and state.current_tick < stop_boundary <= to_tick and (
            stop_boundary < to_tick or external is None and reason not in {"npc_work_boundary", "npc_routine_boundary"}
        ):
            to_tick = stop_boundary
            reason = "stop_condition_boundary"
            reference_id = subject_id = None

        return self._advance_world_window(
            state, plan, context, to_tick, reason, reference_id, subject_id,
        )

    def _npc_routine_hosted(self, context: SimulationContext) -> bool:
        return (
            context.allow_npc_routine_mobility and context.command_actor_id is not None
            and callable(getattr(self.rule_engine, "evaluate_travel_departure", None))
        )

    @staticmethod
    def _npc_routine_exclusions(plan: SimulationPlan, context: SimulationContext) -> frozenset[UUID]:
        controlled = context.externally_controlled_actor_ids
        return controlled | {plan.actor_id} if isinstance(plan, LongActionPlan) else controlled

    def _next_npc_routine_tick(self, state, horizon, plan, context) -> int | None:
        if not self._npc_routine_hosted(context):
            return None
        return next_npc_routine_tick(state, horizon, excluded_actor_ids=self._npc_routine_exclusions(plan, context))

    def _recent_hosted_trip_during_rest(self, state: WorldState, plan: LongActionPlan, context: SimulationContext) -> bool:
        if not self._npc_routine_hosted(context) or plan.started_tick is None or plan.started_tick >= state.current_tick:
            return False
        excluded = self._npc_routine_exclusions(plan, context)
        earliest = max(plan.started_tick, state.current_tick - 59)
        return any(
            journey.actor_id not in excluded
            and (journey.departed_tick >= earliest or journey.resolved_tick is not None and journey.resolved_tick >= earliest)
            and "routine_mobility" in state.entities[journey.actor_id].components
            for journey in state.travel_journeys
        )

    def _with_npc_routine_boundary(self, state, horizon, plan, context, external):
        limit = min(horizon, external[0]) if external is not None else horizon
        routine_tick = self._next_npc_routine_tick(state, limit, plan, context)
        if routine_tick is not None and (external is None or routine_tick < external[0]):
            return routine_tick, "npc_routine_boundary", None, None
        return external

    def _with_npc_work_boundary(self, state, horizon, plan, context, external):
        if context.work_job_id is None or not context.allow_npc_routine_mobility:
            return external
        limit = min(horizon, external[0]) if external is not None else horizon
        tick = next_npc_work_tick(state, limit, excluded_actor_ids=self._npc_routine_exclusions(plan, context))
        if tick is not None and (external is None or tick < external[0]):
            return tick, "npc_work_boundary", None, None
        return external

    def _advance_attended_work(
        self, state: WorldState, plan: LongActionPlan, context: SimulationContext, attendance: NpcAttendance,
    ) -> SimulationResolution:
        end = attendance.ends_at_tick
        if state.current_tick == end:
            self._reject_unconsumed_decisions(context)
            payment = plan_attended_work_shift(state, attendance)
            return self._resolve_npc_work(state, plan, context, NpcWorkTransition("settled", attendance, payment))
        if not npc_attendance_is_valid(state, attendance):
            self._reject_unconsumed_decisions(context)
            return self._resolve_npc_work(state, plan, context, NpcWorkTransition("cancelled", attendance))
        routine = self._resolve_npc_routine(state, plan, context)
        if routine is not None:
            return routine
        meeting = self.resolve_autonomous_meeting_attendance(state, plan, context)
        if meeting is not None:
            return meeting
        horizon = min(end, state.current_tick + context.max_step_ticks,
                      state.current_tick + self.policy.max_macro_step_seconds)
        boundary = self._next_external_boundary(
            state, horizon, actor_id=plan.actor_id,
            externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            include_meeting_start=context.allow_autonomous_meeting_attendance,
        )
        boundary = self._with_npc_work_boundary(state, horizon, plan, context, boundary)
        boundary = self._with_npc_routine_boundary(state, horizon, plan, context, boundary)
        tick, reason, reference, subject = boundary or (horizon, "work_attendance_boundary", None, plan.actor_id)
        return self._advance_to_boundary(state, plan, context, tick, reason, reference, subject, "work",
                                         work_job_id=attendance.work_job_id)

    def _advance_stationary_action(self, state, plan, context, journey):
        routine = self._resolve_npc_routine(state, plan, context)
        if routine is not None:
            return routine
        horizon = min(plan.target_tick, state.current_tick + context.max_step_ticks,
                      state.current_tick + self.policy.max_macro_step_seconds, state.current_tick + 30 * state.calendar.day_seconds)
        if plan.activity == "study":
            horizon = min(horizon, state.current_tick + 8 * 60 * 60)
        boundary = self._next_external_boundary(
            state, horizon, actor_id=plan.actor_id, externally_controlled_actor_ids=context.externally_controlled_actor_ids,
            include_meeting_start=context.allow_autonomous_meeting_attendance,
            deferred_travel_schedule_id=travel_resolution_schedule_id(journey.journey_id) if journey else None,
        )
        if context.allow_autonomous_vacancy:
            from .autonomous_vacancy import next_vacancy_year_boundary

            year_tick = next_vacancy_year_boundary(state, horizon)
            if year_tick is not None and (boundary is None or year_tick < boundary[0]):
                boundary = (year_tick, "autonomous_vacancy_year_boundary", None, None)
        boundary = self._with_npc_routine_boundary(state, horizon, plan, context, boundary)
        tick, reason, reference, subject = boundary or (horizon, "stationary_action_window", None, plan.actor_id)
        return self._advance_to_boundary(state, plan, context, tick, reason, reference, subject, plan.activity)

    def _resolve_npc_routine(
        self, state: WorldState, plan: SimulationPlan, context: SimulationContext, *,
        protected_interval: tuple[int, int] | None = None,
    ) -> SimulationResolution | None:
        if not self._npc_routine_hosted(context):
            return None
        resolution = plan_npc_routine_departure(
            state, RuleContext(
                world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
                actor_id=context.command_actor_id, ruleset_version=context.ruleset_version,
            ), excluded_actor_ids=self._npc_routine_exclusions(plan, context), rules=self.rule_engine,
        )
        if resolution is None:
            return None
        if protected_interval is not None:
            # Work and study preserve their original settlement intervals.
            # The routine may resolve at either endpoint.
            departure = next(event for event in resolution.events if isinstance(event, TravelJourneyChangedEvent))
            journey = departure.journey
            due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
            if protected_interval[0] < due < protected_interval[1]:
                return None
        self._reject_unconsumed_decisions(context)
        self._validate_rule_batch(state, resolution)
        return self._resolved(
            state, context, resolution, outcome="advanced", boundary="npc_routine_departed",
            interruption=None, continuation=plan,
        )

    def resolve_autonomous_meeting_attendance(
        self, state: WorldState, plan: SimulationPlan, context: SimulationContext,
    ) -> SimulationResolution | None:
        if not context.allow_autonomous_meeting_attendance or not state.relationship_invitations:
            return None
        controlled = context.externally_controlled_actor_ids
        if isinstance(plan, LongActionPlan):
            controlled = controlled | {plan.actor_id}
        candidate = next_npc_meeting_attendance_candidate(state, externally_controlled_actor_ids=controlled)
        if candidate is None:
            return None
        request = npc_meeting_attendance_request(candidate, context.command_id, context.state_version)
        response = self._matching_response(context, request)
        if response is None:
            return self._decision_gate(state, context, request, continuation=plan)
        if (response.source != "model" or response.option_id not in {"attend", "defer"}
                or response.npc_planning_status is not None):
            raise StaleDecisionResponse("An NPC attendance request requires one of its model-owned choices.")

        from world_engine.domain.npc_meeting_attendance import build_npc_meeting_attendance_decision_events

        events = build_npc_meeting_attendance_decision_events(
            state, world_id=context.world_id, branch_id=context.branch_id,
            command_id=context.command_id, actor_id=candidate.actor_id, ruleset_version=context.ruleset_version,
            invitation_id=candidate.invitation_id, source_invitation_revision=candidate.expected_invitation_revision,
            choice=response.option_id,
        )
        resolution = RuleResolution(
            events=list(events), elapsed_ticks=0, ruleset_version=context.ruleset_version,
            outcome=RuleOutcome(status="succeeded", code=NPC_MEETING_ATTENDANCE_DECISION_KIND,
                                message="An NPC meeting decision was recorded."),
        )
        return self._resolved(
            state, context, resolution, outcome="advanced", boundary="npc_meeting_attendance_resolved",
            interruption=None, continuation=plan, accepted_decisions=(response,),
        )

    def _resolve_npc_work(
        self, state: WorldState, plan: SimulationPlan, context: SimulationContext, work: NpcWorkTransition,
    ) -> SimulationResolution:
        if context.command_actor_id is None:
            return self._empty(
                state, context, outcome="rule_required", reason="npc_work_command_actor_required",
                interruption=Interruption(kind="rule_required", tick=state.current_tick,
                                          reason_code="npc_work_command_actor_required"),
            )
        fields = {
            "event_id": npc_attendance_event_id(context.command_id, context.ruleset_version,
                                                work.operation, work.attendance.assignment_id),
            "world_id": context.world_id, "branch_id": context.branch_id,
            "command_id": context.command_id, "actor_id": context.command_actor_id,
            "ruleset_version": context.ruleset_version, "attendance": work.attendance,
        }
        if work.operation == "started":
            event = NpcAttendanceStartedEvent(**fields)
        elif work.operation == "cancelled":
            event = NpcAttendanceCancelledEvent(**fields, effective_tick=state.current_tick)
        else:
            event = NpcAttendanceSettledEvent(**fields, effective_tick=state.current_tick, payment=work.payment)
        boundary = f"npc_work_{work.operation}" if work.attendance.work_job_id is None else {
            "started": "work_attendance_started", "cancelled": "work_shift_interrupted",
            "settled": "work_shift_complete" if work.payment is not None else "work_shift_not_paid",
        }[work.operation]
        resolution = RuleResolution(
            events=[event], outcome=RuleOutcome(status="succeeded", code=boundary, message="Work attendance updated."),
            elapsed_ticks=0, ruleset_version=context.ruleset_version,
        )
        self._validate_rule_batch(state, resolution)
        if boundary in {"work_shift_interrupted", "work_shift_not_paid"}:
            return self._resolved(state, context, resolution, outcome="stopped", boundary=boundary,
                                  interruption=Interruption(kind="boundary", tick=state.current_tick,
                                                            reason_code=boundary, subject_id=work.attendance.actor_id))
        return self._resolved(state, context, resolution, outcome="advanced", boundary=boundary,
                              interruption=None, continuation=plan)

    def _resolve_npc_plan_execution(self, state, continuation, context, plan_id):
        from .npc_plan_execution import build_npc_plan_execution_transition
        from world_engine.domain.npc_plan_events import NpcPlanRevisedEvent, npc_plan_event_id
        from world_engine.domain.npc_plan_review import with_npc_failure_review

        self._reject_unconsumed_decisions(context)
        transition = build_npc_plan_execution_transition(
            state, plan_id, context, rule_engine=self.rule_engine, registry=self.npc_capability_registry,
            journey_resolution_receipts=context.journey_resolution_receipts,
        )
        events = list(transition.events)
        if transition.outcome == "interval_required":
            # The interval adapter must own shared-clock accounting. Record an
            # explicit blocked plan instead of applying a player's time-advance
            # events independently for every NPC.
            old = next(item for item in state.npc_plans if item.plan_id == plan_id)
            next_review = state.current_tick + state.calendar.day_seconds
            if next_review > 2**63 - 1:
                raise ValueError("NPC plan retry exceeds the world-clock range.")
            blocked = old.model_copy(update={
                "revision": old.revision + 1, "status": "blocked", "updated_tick": state.current_tick,
                "next_review_tick": next_review, "active_execution": None, "attempts": old.attempts + 1,
                "last_failure": "npc_interval_adapter_required",
                "reason": "This capability requires a registered shared-clock interval executor.",
            })
            events = [with_npc_failure_review(state, NpcPlanRevisedEvent(
                event_id=npc_plan_event_id(context.command_id, context.ruleset_version, "revised", plan_id, blocked.revision),
                world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
                actor_id=old.actor_id, ruleset_version=context.ruleset_version,
                expected_revision=old.revision, plan=blocked,
            ))]
        if not events:
            return self._empty(
                state, context, outcome="rule_required", reason="npc_plan_execution_not_resolved",
                interruption=Interruption(kind="rule_required", tick=state.current_tick,
                                          reason_code="npc_plan_execution_not_resolved", reference_id=plan_id),
            )
        if any(isinstance(event, TimeAdvancedEvent) for event in events):
            raise ValueError("NPC plan execution must use the shared world clock.")
        resolution = RuleResolution(
            events=events, elapsed_ticks=0, ruleset_version=context.ruleset_version,
            outcome=RuleOutcome(status="succeeded", code="npc_plan_execution_resolved", message="An NPC plan transition was recorded."),
        )
        self._validate_rule_batch(state, resolution)
        return self._resolved(state, context, resolution, outcome="advanced", boundary="npc_plan_execution_resolved",
                              interruption=None, continuation=continuation)

    def _advance_world_window(
        self,
        state: WorldState,
        plan: WorldAdvancePlan,
        context: SimulationContext,
        to_tick: int,
        reason: str,
        reference_id: UUID | None,
        subject_id: UUID | None,
    ) -> SimulationResolution:
        self._reject_unconsumed_decisions(context)
        resolver = getattr(self.rule_engine, "resolve_background_window", None)
        if not callable(resolver):
            return self._empty(
                state, context, outcome="rule_required", reason="background_window_resolver_unavailable",
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick,
                    reason_code="background_window_resolver_unavailable",
                ),
            )
        owner_actor_id = context.command_actor_id or context.world_id
        command_id = uuid5(
            context.command_id,
            f"{context.world_seed}:simulation:{context.state_version}:{state.current_tick}:world-advance:{to_tick}",
        )
        rule_context = RuleContext(
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=command_id,
            actor_id=owner_actor_id,
            ruleset_version=context.ruleset_version or RULES_VERSION,
        )
        resolution = resolver(
            state,
            from_tick=state.current_tick,
            to_tick=to_tick,
            context=rule_context,
            choices=(),
            include_actor=True,
        )
        if resolution.outcome.status == "rejected" and resolution.outcome.code == "background_deprivation_pending":
            proposed_boundary = resolution.outcome.details.get("boundary_tick")
            if isinstance(proposed_boundary, int) and state.current_tick < proposed_boundary < to_tick:
                # The rule engine refused the broad routine interval because
                # finite stock runs out before an exact deprivation tick. Retry
                # only to that recorded boundary; the following zero-time pass
                # resolves damage before another routine window can begin.
                to_tick = proposed_boundary
                reason = "background_deprivation_boundary"
                reference_id = None
                subject_id = None
                boundary_command_id = uuid5(
                    context.command_id,
                    f"{context.world_seed}:simulation:{context.state_version}:{state.current_tick}:routine-boundary:{to_tick}",
                )
                boundary_context = rule_context.model_copy(update={"command_id": boundary_command_id})
                resolution = resolver(
                    state,
                    from_tick=state.current_tick,
                    to_tick=to_tick,
                    context=boundary_context,
                    choices=(),
                    include_actor=True,
                )

        scheduled = next((item for item in state.scheduled_events if item.id == reference_id), None) if reference_id else None
        events = list(resolution.events)
        actual_tick = state.current_tick + resolution.elapsed_ticks
        if scheduled is not None and actual_tick == to_tick and _PHASE_ORDER[scheduled.phase] < _PHASE_ORDER["resolution"]:
            # The scheduled phase owns same-tick ordering; close lifespans only
            # after the earlier phase has committed.
            events = [event for event in events if not isinstance(event, LifeEndedEvent)]
            if resolution.outcome.code == "lifespan_expired":
                resolution = resolution.model_copy(update={
                    "events": events,
                    "outcome": RuleOutcome(
                        status="succeeded", code="scheduled_event_boundary",
                        message="The earlier scheduled phase must resolve before lifespan closures at this tick.",
                        details={"scheduled_event_id": str(scheduled.id)},
                    ),
                })
        self._validate_rule_batch(state, resolution)
        actual_tick = state.current_tick + resolution.elapsed_ticks
        continuation = self._continuation(plan, state.current_tick)
        if resolution.outcome.status in {"rejected", "failed"}:
            return self._resolved(
                state, context, resolution, outcome="rule_required", boundary=resolution.outcome.code,
                interruption=Interruption(
                    kind="rule_required", tick=actual_tick,
                    reason_code=resolution.outcome.code, subject_id=subject_id, reference_id=reference_id,
                ),
            )
        if actual_tick < to_tick:
            return self._resolved(
                state, context, resolution, outcome="stopped", boundary="rule_window_ended_early",
                interruption=Interruption(
                    kind="boundary", tick=actual_tick, reason_code="rule_window_ended_early",
                    subject_id=subject_id,
                ),
            )
        if scheduled is not None and actual_tick == scheduled.due_tick:
            # A due scheduled event is a hard boundary even when it coincides
            # with the requested target. The next lease iteration resolves its
            # phase before the job may complete.
            return self._resolved(
                state, context, resolution, outcome="advanced", boundary=reason,
                interruption=None, continuation=continuation,
            )
        exact_deprivation_due = (
            isinstance(resolution.outcome.details.get("boundary_tick"), int)
            and resolution.outcome.details["boundary_tick"] == actual_tick
        )
        if reason in {"background_deprivation_boundary", "npc_work_boundary", "npc_routine_boundary", "npc_plan_due",
                      "autonomous_vacancy_year_boundary", "npc_meeting_start"} or exact_deprivation_due or any(
            isinstance(event, LifeEndedEvent) for event in resolution.events
        ):
            # Damage/death cleanup and any same-tick succession gate are part
            # of the requested interval's boundary, even when it is the target.
            # Keep the lease alive for the zero-time follow-up pass.
            return self._resolved(
                state, context, resolution, outcome="advanced", boundary=reason,
                interruption=None, continuation=continuation,
            )
        if reason == "stop_condition_boundary" or any(
            self._stop_satisfied_after(state, tuple(resolution.events), condition)
            for condition in plan.stop_conditions
        ):
            return self._resolved(
                state, context, resolution, outcome="stopped", boundary="stop_condition_satisfied",
                interruption=Interruption(
                    kind="stop_condition", tick=actual_tick, reason_code="stop_condition_satisfied",
                ),
            )
        if actual_tick >= plan.target_tick:
            return self._resolved(
                state, context, resolution, outcome="completed", boundary="target_reached",
                interruption=None,
            )
        # Keep the job leased across normal time, care, schedule, and death
        # boundaries so the next deterministic resolution runs at that tick.
        return self._resolved(
            state, context, resolution, outcome="advanced", boundary=reason,
            interruption=None, continuation=continuation,
        )

    def _action(self, state, plan: LongActionPlan, activity: str, food_resource: str | None, water_resource: str | None):
        if activity == "wait":
            duration = max(1, self.policy.max_macro_step_seconds)
            return (WaitIntent(duration_ticks=duration), duration, False)
        if activity == "rest":
            duration = max(60, self.policy.max_macro_step_seconds)
            return (RestIntent(duration_ticks=duration), duration, False)
        if activity == "eat":
            if food_resource is None:
                return None
            return (ConsumeIntent(need="food", resource_id=food_resource, quantity="1"), 300, True)
        if activity == "drink":
            if water_resource is None:
                return None
            return (ConsumeIntent(need="water", resource_id=water_resource, quantity="1"), 60, True)
        if activity == "travel":
            if plan.route_id is None:
                raise ValueError("Travel action is missing its route id.")
            actor = state.entities[plan.actor_id]
            if actor.location_id is None:
                raise ValueError("A character must have a current location before travel.")
            origin = state.entities[actor.location_id]
            from world_engine.domain.components import PlaceComponent

            place = PlaceComponent.model_validate(origin.components.get("place"))
            route = next((item for item in place.routes if item.route_id == plan.route_id), None)
            if route is None:
                raise ValueError("The selected route is not registered at the actor's current location.")
            return (MoveIntent(destination_id=route.to_place_id, route_id=route.route_id), route.travel_ticks, True)
        if activity == "study":
            if plan.skill_id is None:
                raise ValueError("Study action is missing skill_id.")
            duration = min(8 * 60 * 60, state.calendar.day_seconds, self.policy.max_macro_step_seconds)
            return (TrainIntent(skill_id=plan.skill_id, duration_ticks=max(1, duration)), duration, False)
        if activity == "produce":
            if plan.producer_id is None or plan.recipe_id is None:
                raise ValueError("Production action is missing producer_id or recipe_id.")
            producer = state.entities.get(plan.producer_id)
            if producer is None:
                raise ValueError("Production site does not exist.")
            from world_engine.domain.components import ProductionComponent

            production = ProductionComponent.model_validate(producer.components.get("production"))
            recipe = next((item for item in production.recipes if item.recipe_id == plan.recipe_id), None)
            if recipe is None:
                raise ValueError("Production recipe is not registered for this producer.")
            return (ProduceIntent(producer_id=plan.producer_id, recipe_id=plan.recipe_id), recipe.duration_ticks, False)
        return None

    @staticmethod
    def _routine_work_details(
        state: WorldState, plan: LongActionPlan,
    ) -> tuple[WorkShiftTerms, UUID, UUID] | str:
        """Resolve registered shift terms and the worker's canonical wage account."""

        schedule = plan.work_schedule
        if schedule is None:
            return "work_schedule_missing"
        assignments = [
            item for item in state.work_assignments
            if item.assignment_id == schedule.assignment_id
            and item.actor_id == plan.actor_id
            and item.ended_tick is None
        ]
        if len(assignments) != 1:
            return "work_assignment_unavailable"
        assignment = assignments[0]
        try:
            terms = registered_work_shift_terms(state).get(assignment.posting_id)
        except WorkShiftError:
            return "work_shift_terms_invalid"
        if terms is None:
            return "work_shift_terms_unavailable"
        account_id = select_wage_holder_account(state, plan.actor_id, terms.resource_id)
        if account_id is None:
            return "work_wage_account_unavailable"
        return terms, account_id, assignment.location_id

    @staticmethod
    def _work_purchase_quote(
        state: WorldState,
        target: WorkPurchaseTarget,
        *,
        actor_id: UUID,
        workplace_id: UUID | None,
        wage_resource_id: str,
    ) -> Decimal:
        """Validate the target against the assignment and wage currency."""

        if workplace_id is None or target.market_id != workplace_id:
            raise ValueError("purchase target market is not at the job workplace")
        if target.currency_resource_id != wage_resource_id:
            raise ValueError("purchase target currency does not match the wage account")
        actor = state.entities.get(actor_id)
        market = state.entities.get(target.market_id)
        if actor is None or market is None:
            raise ValueError("purchase target actor or market is unavailable")
        observed = _observed_entities(state, actor_id)
        if market.components.get("hidden") is True and market.id not in observed:
            raise ValueError("purchase target market is not known to the worker")
        seller = state.entities.get(target.seller_id)
        if (
            seller is not None and seller.id != actor.id
            and seller.components.get("hidden") is True and seller.id not in observed
        ):
            raise ValueError("purchase target seller is not known to the worker")
        return observe_work_purchase_quote(state, target)

    @staticmethod
    def _next_routine_work_tick(
        current_tick: int,
        *,
        first_shift_tick: int,
        cadence_ticks: int,
        shift_duration: int,
    ) -> tuple[int | None, str | None]:
        """Find the next scheduled start and detect a missed atomic shift.

        A clock position inside a scheduled shift means the start was missed;
        the engine has no persisted work-in-progress contract to resume it.
        Clock positions at or beyond a scheduled completion are treated as
        completed only because this runner commits the shift's time and wage
        events atomically, or stops before moving past its start.
        """

        if current_tick < first_shift_tick:
            return first_shift_tick, None
        elapsed = current_tick - first_shift_tick
        if elapsed == 0:
            return first_shift_tick, None
        if elapsed < shift_duration:
            return None, "work_shift_start_missed"
        latest_completed_index = (elapsed - shift_duration) // cadence_ticks
        next_index = latest_completed_index + 1
        next_tick = first_shift_tick + next_index * cadence_ticks
        if next_tick > 2**63 - 1:
            return None, None
        if next_tick < current_tick:
            return next_tick, "work_shift_start_missed"
        return next_tick, None

    def _routine_work_rest_plan(
        self, state: WorldState, actor_id: UUID, shift_start_tick: int,
    ) -> tuple[int, int] | None:
        """Schedule rest with a policy-level buffer before the next shift.

        The prediction uses the same integer rates and retained remainder as
        the registered rules. The selected interval is later executed through
        RestIntent; this helper never changes needs or advances world time. A
        due stocked meal or drink is treated as a competing care window, so
        rest starts early enough to leave a buffer after that care.
        """

        actor = state.entities.get(actor_id)
        raw_needs = actor.components.get("needs") if actor is not None else None
        if not isinstance(raw_needs, dict) or shift_start_tick <= state.current_tick:
            return None
        needs = NeedsComponent.model_validate(raw_needs)
        need = needs.rest
        if need is None:
            return None
        interval = shift_start_tick - state.current_tick
        day_seconds = state.calendar.day_seconds

        care_windows = self._routine_work_care_windows(state, actor_id, shift_start_tick)
        care_ticks = sum(duration for _tick, duration in care_windows)
        policy_threshold = int(getattr(self.agent_policy, "rest_when_below", 30))
        care_reserve = (
            (PASSIVE_NEED_RATES_PER_DAY["rest"] * care_ticks + day_seconds - 1) // day_seconds
            if care_ticks else 0
        )
        threshold = min(99, policy_threshold + care_reserve)

        def advance(value: int, remainder: int, duration: int, rate_per_day: int) -> tuple[int, int]:
            numerator = rate_per_day * duration + remainder
            change = self._truncate_division(numerator, day_seconds)
            next_remainder = numerator - change * day_seconds
            next_value = max(0, min(100, value - change))
            return next_value, next_remainder

        waited_value, _ = advance(need.value, need.decay_remainder, interval, 24)
        if waited_value > threshold or interval < 60:
            return None

        # If stocked food or water will be used before the shift, rest before
        # that one-shot action so the meal's passive time cannot consume the
        # final rest buffer at the scheduled start.
        rest_first = need.value <= threshold or bool(care_windows)

        def value_after_rest(rest_ticks: int) -> int:
            wait_ticks = interval - rest_ticks
            if rest_first:
                value, remainder = advance(need.value, need.decay_remainder, rest_ticks, -36)
                value, _ = advance(value, remainder, wait_ticks, 24)
            else:
                value, remainder = advance(need.value, need.decay_remainder, wait_ticks, 24)
                value, _ = advance(value, remainder, rest_ticks, -36)
            return value

        if value_after_rest(interval) > threshold:
            low, high = 60, interval
            while low < high:
                middle = (low + high) // 2
                if value_after_rest(middle) > threshold:
                    high = middle
                else:
                    low = middle + 1
            duration = low
        else:
            # The longest registered rest still leaves the actor below the
            # threshold. Rest for all available time, then let the due-shift
            # gate request player input if the need remains critical.
            duration = interval

        rest_start_tick = state.current_tick if rest_first else shift_start_tick - duration
        return rest_start_tick, duration

    def _routine_work_care_windows(
        self, state: WorldState, actor_id: UUID, shift_start_tick: int,
    ) -> tuple[tuple[int, int], ...]:
        """List stocked food and water actions expected before a work start."""

        actor = state.entities.get(actor_id)
        raw_needs = actor.components.get("needs") if actor is not None else None
        if not isinstance(raw_needs, dict):
            return ()
        needs = NeedsComponent.model_validate(raw_needs)
        roles = state.metadata.get("resource_roles")
        if not isinstance(roles, dict):
            return ()
        candidates: list[tuple[int, int]] = []
        for name, rate_per_day, duration in (
            ("water", PASSIVE_NEED_RATES_PER_DAY["water"], 60),
            ("food", PASSIVE_NEED_RATES_PER_DAY["food"], 300),
        ):
            need = getattr(needs, name)
            resource_id = roles.get(f"{name}_resource_id")
            if need is None or not isinstance(resource_id, str):
                continue
            threshold = min(
                int(getattr(self.agent_policy, "eat_when_below", 25)),
                need.critical_below,
            )
            care_tick = next_need_threshold_tick(
                need,
                threshold=threshold,
                rate_per_day=rate_per_day,
                from_tick=state.current_tick,
                day_seconds=state.calendar.day_seconds,
            )
            if care_tick <= shift_start_tick and self._owned_inventory_quantity(
                state, actor_id, resource_id, at_tick=care_tick,
            ) >= 1:
                candidates.append((care_tick, duration))
        return tuple(candidates)

    @staticmethod
    def _owned_inventory_quantity(
        state: WorldState, actor_id: UUID, resource_id: str, *, at_tick: int,
    ):
        """Return stock the canonical consume rule can draw from the actor."""

        actor = state.entities.get(actor_id)
        raw_inventory = actor.components.get("inventory") if actor is not None else None
        if raw_inventory is None:
            return 0
        inventory = InventoryComponent.model_validate(raw_inventory)
        # RuleEngine._holder_account selects the first matching account in
        # state order, so the forecast must require inventory on that account.
        selected = next((
            (account_id, account)
            for account_id, account in state.resources.items()
            if account.account_type == "holder"
            and account.owner_id == actor_id
            and account.resource_id == resource_id
        ), None)
        if selected is None:
            return 0
        account_id, account = selected
        lot_quantity = sum((
            lot.quantity for lot in inventory.lots
            if lot.account_id == account_id
            and lot.resource_id == resource_id
            and lot.quantity > 0
            and (lot.expiry_tick is None or lot.expiry_tick > at_tick)
        ), start=0)
        return min(account.balance, lot_quantity)

    @staticmethod
    def _truncate_division(value: int, divisor: int) -> int:
        """Match the rule engine's signed, toward-zero rate integration."""

        return value // divisor if value >= 0 else -((-value) // divisor)

    @staticmethod
    def _with_duration(intent, duration: int):
        raw = intent.model_dump(mode="python")
        raw["duration_ticks"] = duration
        return type(intent).model_validate(raw)

    def _next_external_boundary(
        self,
        state: WorldState,
        horizon: int,
        *,
        actor_id: UUID | None = None,
        externally_controlled_actor_ids: frozenset[UUID] = frozenset(),
        deferred_travel_schedule_id: UUID | None = None,
        include_meeting_start: bool = False,
    ):
        # (tick, phase rank, negative priority, type rank, stable id, schedule id, subject id)
        candidates: list[tuple[int, int, int, int, str, UUID | None, UUID | None]] = []
        if include_meeting_start:
            meeting_start = next_npc_meeting_attendance_boundary(
                state, horizon, externally_controlled_actor_ids=externally_controlled_actor_ids,
            )
            if meeting_start is not None:
                candidates.append((meeting_start, 2 * max(_PHASE_ORDER.values()) + 3,
                                   0, 5, "npc-meeting-start", None, None))
        for npc_plan in state.npc_plans:
            if npc_plan.status not in {"ready", "running", "blocked"} or npc_plan.next_review_tick is None:
                continue
            due_tick = npc_plan.next_review_tick
            actor = state.entities.get(npc_plan.actor_id)
            if actor is None or actor.status != "active":
                due_tick = state.current_tick
            if (
                isinstance(npc_plan.active_execution, NpcTimedExecution)
                and npc_plan.actor_id in externally_controlled_actor_ids
            ):
                due_tick = state.current_tick
            for goal in state.goals:
                if goal.id not in npc_plan.goal_ids:
                    continue
                if npc_goal_fulfillment_allows_continuation(state, goal):
                    continue
                if goal.status not in {"active", "succeeded"}:
                    due_tick = state.current_tick
                elif goal.deadline is not None and goal.deadline < 2**63 - 1:
                    due_tick = min(due_tick, goal.deadline + 1)
            boundary_tick = max(state.current_tick, due_tick)
            if boundary_tick <= horizon:
                # Registered NPC planning/execution runs after the existing
                # world-boundary phases, so a due maintenance barrier cannot
                # be mistaken for a failed NPC action. No clock is advanced.
                candidates.append((boundary_tick, 2 * max(_PHASE_ORDER.values()) + 2, 0, 4, str(npc_plan.plan_id), npc_plan.plan_id, npc_plan.actor_id))
        for event in state.scheduled_events:
            if event.id == deferred_travel_schedule_id and event.due_tick > state.current_tick:
                continue
            boundary_tick = max(state.current_tick, event.due_tick)
            if event.status == "queued" and boundary_tick <= horizon:
                reason = "scheduled_decision_requires_resolution" if event.phase == "decision" else "scheduled_event_requires_registered_handler"
                phase_rank = (2 * max(_PHASE_ORDER.values()) + 1
                              if event.handler_id == "npc.goal_review" else 2 * _PHASE_ORDER[event.phase])
                candidates.append(
                    (boundary_tick, phase_rank, -event.priority, 0, str(event.id), event.id, None)
                )
        for entity in state.entities.values():
            if entity.kind not in {"character", "person"} or entity.status in {"dead", "retired"}:
                continue
            life = entity.components.get("life")
            if not isinstance(life, dict) or life.get("lifespan_ticks") is None:
                continue
            expiry_tick = int(life["birth_tick"]) + int(life["lifespan_ticks"])
            boundary_tick = max(state.current_tick, expiry_tick)
            if boundary_tick <= horizon:
                # Lifespan closure is a resolution-phase event. Scheduled
                # environment, delivery, decision, and higher/equal-priority
                # resolution phases get their deterministic turn first.
                candidates.append((boundary_tick, 2 * _PHASE_ORDER["resolution"], 0, 1, str(entity.id), None, entity.id))
        if actor_id is not None:
            care_tick = self.agent_policy.next_background_self_care_boundary(
                state,
                exclude_actor_ids=frozenset({actor_id}),
            )
            if care_tick is not None and state.current_tick < care_tick <= horizon:
                candidates.append(
                    (care_tick, 2 * _PHASE_ORDER["resolution"], 0, 2, "background-self-care", None, None)
                )
        background_boundary_method = getattr(self.rule_engine, "next_background_boundary", None)
        if callable(background_boundary_method):
            background_tick = background_boundary_method(
                state, actor_id=actor_id, include_actor=actor_id is None,
            )
            # Long actions stop at future NPC deprivation ticks. World advance
            # lets the closed-form routine event detect interior stock failure
            # and retains only already-due damage cleanup as an external gate.
            should_include_background = (
                background_tick is not None
                and (
                    background_tick <= state.current_tick
                    or actor_id is not None and background_tick <= horizon
                )
            )
            if should_include_background:
                candidates.append(
                    (
                        background_tick,
                        2 * _PHASE_ORDER["resolution"],
                        0,
                        3,
                        "background-deprivation",
                        None,
                        None,
                    )
                )
        if not candidates:
            return None
        tick, _phase, _priority, kind_order, _stable, reference, subject = min(candidates)
        if kind_order == 4:
            reason = "npc_plan_due"
        elif kind_order == 5:
            reason = "npc_meeting_start"
        elif kind_order == 2:
            reason = "background_self_care_boundary"
        elif kind_order == 3:
            reason = "background_deprivation_pending" if tick <= state.current_tick else "background_deprivation_boundary"
        elif kind_order == 1:
            reason = "life_expiry_requires_death_and_succession_rules"
        else:
            reason = (
            "scheduled_decision_requires_resolution"
            if next(event for event in state.scheduled_events if event.id == reference).phase == "decision"
            else "scheduled_event_requires_registered_handler"
            )
        return tick, reason, reference, subject

    def _next_actor_self_care_boundary(
        self,
        state: WorldState,
        actor_id: UUID,
        *,
        food_available: bool,
        water_available: bool,
    ) -> int | None:
        """Find when the focal actor's stocked food or water becomes policy-critical."""

        actor = state.entities.get(actor_id)
        if actor is None:
            return None
        raw_needs = actor.components.get("needs")
        if raw_needs is None:
            return None
        needs = NeedsComponent.model_validate(raw_needs)
        eat_threshold = int(getattr(self.agent_policy, "eat_when_below", 25))
        candidates: list[int] = []
        if needs.food is not None and food_available:
            food_tick = next_need_threshold_tick(
                needs.food,
                threshold=min(eat_threshold, needs.food.critical_below),
                rate_per_day=PASSIVE_NEED_RATES_PER_DAY["food"],
                from_tick=state.current_tick,
                day_seconds=state.calendar.day_seconds,
            )
            if food_tick > state.current_tick:
                candidates.append(food_tick)
        if needs.water is not None and water_available:
            water_tick = next_need_threshold_tick(
                needs.water,
                threshold=min(eat_threshold, needs.water.critical_below),
                rate_per_day=PASSIVE_NEED_RATES_PER_DAY["water"],
                from_tick=state.current_tick,
                day_seconds=state.calendar.day_seconds,
            )
            if water_tick > state.current_tick:
                candidates.append(water_tick)
        return min(candidates) if candidates else None

    @staticmethod
    def _expiring_actors(state: WorldState, tick: int) -> tuple[UUID, ...]:
        matches: list[UUID] = []
        for entity in state.entities.values():
            if entity.kind not in {"character", "person"} or entity.status in {"dead", "retired"}:
                continue
            life = entity.components.get("life")
            if not isinstance(life, dict) or life.get("lifespan_ticks") is None:
                continue
            if int(life["birth_tick"]) + int(life["lifespan_ticks"]) <= tick:
                matches.append(entity.id)
        return tuple(sorted(matches, key=str))

    @staticmethod
    def _population_revision(state: WorldState, actor) -> int | None:
        life = actor.components.get("life")
        if not isinstance(life, dict) or life.get("population_group_id") is None:
            return None
        group_id = UUID(str(life["population_group_id"]))
        group = state.entities.get(group_id)
        if group is None:
            return None
        population = group.components.get("population")
        return int(population["revision"]) if isinstance(population, dict) else None

    @staticmethod
    def _continuation(plan: SimulationPlan, current_tick: int) -> SimulationPlan:
        return plan.model_copy(
            update={"started_tick": current_tick if plan.started_tick is None else plan.started_tick}
        )

    def _resolve_current_lifespan_boundary(
        self, state: WorldState, context: SimulationContext, subject_id: UUID | None, plan: SimulationPlan
    ) -> SimulationResolution:
        self._reject_unconsumed_decisions(context)
        events: list[DomainEvent] = []
        projected = state
        for actor_id in self._expiring_actors(state, state.current_tick):
            actor = projected.entities[actor_id]
            life = LifeComponent.model_validate(actor.components.get("life"))
            event = LifeEndedEvent(
                entity_id=actor_id,
                expected_life_revision=life.revision,
                expected_population_revision=self._population_revision(projected, actor),
                event_id=self._event_id(context, state.current_tick, f"life-expired:{actor_id}:{life.revision}"),
            )
            projected = apply_event(projected, event)
            events.append(event)
        if not events:
            return self._empty(
                state,
                context,
                outcome="rule_required",
                reason="life_expiry_requires_registered_life_event",
                interruption=Interruption(kind="rule_required", tick=state.current_tick, reason_code="life_expiry_requires_registered_life_event", subject_id=subject_id),
            )
        resolution = RuleResolution(
            events=events,
            outcome=RuleOutcome(
                status="partial",
                code="lifespan_expired",
                message="One or more characters reached their registered lifespan boundary.",
                details={
                    "expired_character_ids": [
                        str(actor_id) for actor_id in self._expiring_actors(state, state.current_tick)
                    ]
                },
            ),
            elapsed_ticks=0,
            ruleset_version=context.ruleset_version,
        )
        actor_id = plan.actor_id if isinstance(plan, LongActionPlan) else None
        actor_expired = actor_id in {event.entity_id for event in events} if actor_id is not None else False
        return self._resolved(
            state,
            context,
            resolution,
            outcome="stopped" if actor_expired else "advanced",
            boundary="lifespan_expired",
            interruption=Interruption(
                kind="boundary",
                tick=state.current_tick,
                reason_code="actor_lifespan_expired" if actor_expired else "npc_lifespan_expired",
                subject_id=actor_id if actor_expired else subject_id,
            ),
            continuation=None if actor_expired else self._continuation(plan, state.current_tick),
        )

    def _resolve_scheduled_event(
        self,
        state: WorldState,
        scheduled: ScheduledEventRecord,
        context: SimulationContext,
        plan: SimulationPlan,
    ) -> SimulationResolution:
        self._reject_unconsumed_decisions(context)
        if scheduled.handler_id == TRAVEL_RESOLUTION_HANDLER_ID and scheduled.handler_version == TRAVEL_RESOLUTION_HANDLER_VERSION:
            return self._resolve_scheduled_travel(state, scheduled, context, plan)
        # These typed events verify the persisted command ID at storage and replay boundaries.
        if scheduled.handler_id in {
            "shipment.spoilage", "relationship_promise.deadline", PRICE_REVIEW_HANDLER_ID, CREDIT_MATURITY_HANDLER,
        }:
            command_id = context.command_id
        else:
            command_id = uuid5(
                context.command_id,
                f"{context.world_seed}:simulation:{context.state_version}:{state.current_tick}:scheduled:{scheduled.id}",
            )
        resolution = self.rule_engine.resolve_scheduled(
            state,
            scheduled,
            RuleContext(
                world_id=context.world_id,
                branch_id=context.branch_id,
                command_id=command_id,
                actor_id=self._rule_actor_id(plan, context),
                ruleset_version=context.ruleset_version or RULES_VERSION,
            ),
        )
        if resolution.outcome.status == "succeeded":
            return self._resolved(
                state,
                context,
                resolution,
                outcome="advanced",
                boundary=resolution.outcome.code,
                interruption=None,
                continuation=self._continuation(plan, state.current_tick),
            )
        return self._resolved(
            state,
            context,
            resolution,
            outcome="rule_required",
            boundary=resolution.outcome.code,
            interruption=Interruption(
                kind="rule_required",
                tick=state.current_tick,
                reason_code=resolution.outcome.code,
                reference_id=scheduled.id,
            ),
            continuation=plan,
        )

    def _resolve_scheduled_travel(self, state, scheduled, context, plan) -> SimulationResolution:
        journey = next((row for row in state.travel_journeys if str(row.journey_id) == scheduled.payload.get("journey_id")), None)
        if journey is None:
            return self._empty(
                state, context, outcome="rule_required", reason="travel_journey_unavailable",
                interruption=Interruption(kind="rule_required", tick=state.current_tick, reason_code="travel_journey_unavailable"),
            )
        resolution = self.rule_engine.evaluate_travel_resolution(state, journey.journey_id, RuleContext(
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            actor_id=journey.actor_id, ruleset_version=context.ruleset_version,
        ))
        self._validate_rule_batch(state, resolution)
        event = next((item for item in resolution.events if isinstance(item, TravelJourneyChangedEvent)), None)
        own_trip = isinstance(plan, LongActionPlan) and plan.activity == "travel" and plan.actor_id == journey.actor_id
        reason = resolution.outcome.code
        if event is not None and not own_trip:
            return self._resolved(state, context, resolution, outcome="advanced", boundary=reason,
                                  interruption=None, continuation=self._continuation(plan, state.current_tick))
        if event is not None and event.operation == "arrive":
            return self._resolved(state, context, resolution, outcome="completed", boundary=reason, interruption=None)
        return self._resolved(
            state, context, resolution, outcome="stopped" if event is not None else "rule_required",
            boundary=reason, interruption=Interruption(
                kind="boundary" if event is not None else "rule_required", tick=state.current_tick,
                reason_code=reason, subject_id=journey.actor_id,
            ),
        )

    def _resolve_background_deprivation(
        self,
        state: WorldState,
        plan: SimulationPlan,
        context: SimulationContext,
        subject_id: UUID | None,
    ) -> SimulationResolution:
        resolver = getattr(self.rule_engine, "resolve_background_deprivation", None)
        if not callable(resolver):
            return self._empty(
                state,
                context,
                outcome="rule_required",
                reason="background_deprivation_resolver_unavailable",
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick,
                    reason_code="background_deprivation_resolver_unavailable", subject_id=subject_id,
                ),
            )
        command_id = uuid5(
            context.command_id,
            f"{context.world_seed}:simulation:{context.state_version}:{state.current_tick}:background-deprivation",
        )
        resolver_context = RuleContext(
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=command_id,
            actor_id=self._rule_actor_id(plan, context),
            ruleset_version=context.ruleset_version or RULES_VERSION,
        )
        if isinstance(plan, WorldAdvancePlan):
            resolution = resolver(state, resolver_context, include_actor=True)
        else:
            resolution = resolver(state, resolver_context)
        if resolution.outcome.status not in {"succeeded", "partial"} or not resolution.events:
            return self._resolved(
                state,
                context,
                resolution,
                outcome="rule_required",
                boundary=resolution.outcome.code,
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick,
                    reason_code=resolution.outcome.code, subject_id=subject_id,
                ),
            )
        self._validate_rule_batch(state, resolution)
        return self._resolved(
            state,
            context,
            resolution,
            outcome="advanced",
            boundary=resolution.outcome.code,
            interruption=None,
            continuation=self._continuation(plan, state.current_tick),
        )

    @staticmethod
    def _scheduled_handler_supported(scheduled: ScheduledEventRecord) -> bool:
        return (
            (scheduled.handler_id == "production.quota_renewal" and scheduled.handler_version == "1.0.0")
            or (scheduled.handler_id == "population.year_advance" and scheduled.handler_version == "1.0.0")
            or (scheduled.handler_id == "environmental.harvest" and scheduled.handler_version == "1.0.0")
            or (scheduled.handler_id == "shipment.arrival" and scheduled.handler_version == "1.0.0")
            or (scheduled.handler_id == TRAVEL_RESOLUTION_HANDLER_ID and scheduled.handler_version == TRAVEL_RESOLUTION_HANDLER_VERSION)
            or (scheduled.handler_id == "relationship_promise.deadline" and scheduled.handler_version in {
                "1.0.0", PROMISE_TRUST_DEADLINE_HANDLER_VERSION,
            })
            or (scheduled.handler_id == "shipment.spoilage" and scheduled.handler_version == "1.0.0")
            or (scheduled.handler_id == PRICE_REVIEW_HANDLER_ID and scheduled.handler_version == PRICE_REVIEW_HANDLER_VERSION)
            or (scheduled.handler_id == CREDIT_MATURITY_HANDLER and scheduled.handler_version == CREDIT_MATURITY_HANDLER_VERSION)
            or (scheduled.handler_id == STORY_NODE_HANDLER and scheduled.handler_version == STORY_NODE_VERSION)
            or (scheduled.handler_id, scheduled.handler_version) == (MEETING_COMMITMENT_HANDLER, MEETING_COMMITMENT_VERSION)
        )

    @staticmethod
    def _rule_actor_id(plan: SimulationPlan, context: SimulationContext) -> UUID:
        if isinstance(plan, LongActionPlan):
            return plan.actor_id
        # World advance has no focal character. The authenticated world owner is
        # the command actor; world_id is only a deterministic safe fallback for
        # direct pure-engine callers that omit application context.
        return context.command_actor_id or context.world_id

    @staticmethod
    def _next_background_stop_boundary(
        state: WorldState, conditions: tuple[StopCondition, ...]
    ) -> int | None:
        """Calculate exact downward need crossings from the registered integer rates."""
        rates = {"food": 18, "water": 30, "rest": 24}
        day_seconds = state.calendar.day_seconds
        boundaries: list[int] = []
        for condition in conditions:
            if condition.kind != "need_at_or_below" or condition.need not in rates:
                continue
            entity = state.entities.get(condition.subject_id)
            raw = entity.components.get("needs") if entity is not None else None
            if raw is None:
                continue
            needs = NeedsComponent.model_validate(raw)
            need = getattr(needs, condition.need)
            if need is None or need.value <= condition.threshold:
                continue
            required = (need.value - int(condition.threshold)) * day_seconds - need.decay_remainder
            if required <= 0:
                duration = 1
            else:
                rate = rates[condition.need]
                duration = (required + rate - 1) // rate
            boundaries.append(state.current_tick + max(1, duration))
        return min(boundaries) if boundaries else None

    def resolve_autonomous_vacancy(
        self, state: WorldState, plan: SimulationPlan, context: SimulationContext,
    ) -> SimulationResolution | None:
        if not context.allow_autonomous_vacancy:
            return None
        from .autonomous_vacancy import VacancyNomination, VacancyVote, next_autonomous_vacancy_step, resolve_autonomous_vacancy_step

        step = next_autonomous_vacancy_step(state, context)
        if step is None:
            return None
        response = None
        if isinstance(step, (VacancyNomination, VacancyVote)):
            request = step.request
            response = self._matching_response(context, request)
            if response is None:
                return self._decision_gate(state, context, request, continuation=plan)
            if response.source != "model" or response.option_id not in {option.option_id for option in request.options}:
                raise StaleDecisionResponse("Autonomous vacancy response must match its offered NPC options.")
            events = resolve_autonomous_vacancy_step(state, context, step, response.option_id)
        else:
            self._reject_unconsumed_decisions(context)
            events = (step,)
        resolution = RuleResolution(
            events=list(events), elapsed_ticks=0, ruleset_version=context.ruleset_version,
            outcome=RuleOutcome(status="succeeded", code="autonomous_vacancy_processed",
                                message="The registered vacancy proceeding advanced."),
        )
        self._validate_rule_batch(state, resolution)
        return self._resolved(
            state, context, resolution, outcome="advanced", boundary="autonomous_vacancy_processed",
            interruption=None, continuation=plan, accepted_decisions=(response,) if response is not None else (),
        )

    def _pending_succession_request(self, state: WorldState, context: SimulationContext) -> DecisionRequest | None:
        for organization in sorted(state.entities.values(), key=lambda entity: str(entity.id)):
            raw = organization.components.get("governance")
            if raw is None:
                continue
            governance = GovernanceComponent.model_validate(raw)
            dead_leader = governance.leader_id is not None and self._is_terminal(state, governance.leader_id)
            dead_office = next(
                (
                    office for office in sorted(governance.offices, key=lambda item: item.office_id)
                    if office.holder_id is not None and self._is_terminal(state, office.holder_id)
                ),
                None,
            )
            if not dead_leader and dead_office is None:
                continue
            slot = "leader" if dead_leader else f"office_{dead_office.office_id}"
            dead_holder = governance.leader_id if dead_leader else dead_office.holder_id
            active_members = [
                member_id for member_id in governance.members
                if member_id != dead_holder and not self._is_terminal(state, member_id)
            ]
            user_owned = self._player_has_succession_authority(state, governance, active_members)
            from world_engine.domain.autonomous_vacancy import AUTONOMOUS_VACANCY_BALLOT_AUTHORITY

            charter_requires_ballot = (
                dead_leader and governance.succession_policy in {"election", "council"}
                and AUTONOMOUS_VACANCY_BALLOT_AUTHORITY in governance.authority_tags
            )
            if charter_requires_ballot:
                # The administrative clearance creates a vacancy. The election
                # is a separate proceeding with its own electorate and votes.
                user_owned = False
                active_members = []
            if user_owned or governance.succession_policy == "none":
                # Player choices must not reveal unknown hidden members. Keep
                # the legacy candidate snapshot for the leave-only `none`
                # policy until that case receives a separate contract change.
                known_member_ids = self._known_member_ids(state, state.player_id)
                active_members = [
                    member_id for member_id in active_members
                    if state.entities[member_id].components.get("hidden") is not True
                    or member_id in known_member_ids
                ]
            active_members.sort(key=lambda value: (state.entities[value].name or "", str(value)))
            request_id = self._event_id(context, state.current_tick, f"succession:{organization.id}:{slot}:{dead_holder}")
            decision_target = GovernanceDecisionTarget(
                organization_id=organization.id,
                slot="leader" if dead_leader else "office",
                office_id=None if dead_leader else dead_office.office_id,
                predecessor_id=dead_holder,
                decision_basis=governance.succession_policy,
                expected_governance_revision=governance.revision,
                candidate_ids=tuple(active_members[:63]),
            )
            options: list[DecisionOption] = []
            if governance.succession_policy != "none":
                for index, member_id in enumerate(decision_target.candidate_ids, start=1):
                    name = state.entities[member_id].name or f"member {index}"
                    options.append(
                        DecisionOption(
                            option_id=f"{slot}_candidate_{index}",
                            label=f"Appoint {name}",
                        )
                    )
            options.append(DecisionOption(option_id="leave_vacant", label="Leave the leadership slot vacant"))
            slot_title = "leadership" if dead_leader else dead_office.title
            return DecisionRequest(
                request_id=request_id,
                kind="organization.succession",
                decision_owner="user" if user_owned else "model",
                causal_subject_id=dead_holder,
                resolution_target=decision_target,
                actor_id=None,
                at_tick=state.current_tick,
                state_version=context.state_version,
                title=(
                    "Choose an eligible leader for your organization"
                    if user_owned
                    else f"Resolve {slot_title} succession for {organization.name or organization.kind}"
                ),
                reason_code="dead_office_holder_requires_succession",
                options=tuple(options),
                evidence_ids=() if user_owned else tuple(value for value in (organization.id, dead_holder) if value is not None),
            )
        return None

    @staticmethod
    def _player_has_succession_authority(state, governance, active_members: list[UUID]) -> bool:
        player_id = state.player_id
        if player_id is None or player_id not in active_members:
            return False
        if governance.succession_policy in {"election", "council"}:
            return True
        decision_authorities = {
            "authority.appoint",
            "authority.succession.decide",
            "governance.succession.decide",
        }
        return any(
            office.holder_id == player_id
            and bool(decision_authorities.intersection(office.authority))
            for office in governance.offices
        )

    @staticmethod
    def _known_member_ids(state, player_id: UUID | None) -> set[UUID]:
        if player_id is None:
            return set()
        from world_engine.agents.knowledge import build_agent_knowledge

        knowledge = build_agent_knowledge(state, player_id)
        return {
            record.subject_id
            for record in knowledge.known_records
            if record.subject_id is not None
        }

    @staticmethod
    def _is_terminal(state: WorldState, entity_id: UUID) -> bool:
        entity = state.entities.get(entity_id)
        return entity is None or entity.status in {"dead", "retired"}

    def _advance_to_boundary(self, state, plan, context, tick, reason, reference, subject, activity, *, work_job_id=None):
        self._reject_unconsumed_decisions(context)
        duration = tick - state.current_tick
        if duration <= 0:
            return self._empty(
                state,
                context,
                outcome="rule_required",
                reason=reason,
                interruption=Interruption(kind="rule_required", tick=tick, reason_code=reason, subject_id=subject, reference_id=reference),
            )
        slice_command_id = uuid5(
            context.command_id,
            f"{context.world_seed}:simulation:{context.state_version}:{state.current_tick}:boundary:{reason}:{activity}",
        )
        if activity == "study" and plan.skill_id is not None:
            boundary_intent = TrainIntent(skill_id=plan.skill_id, duration_ticks=duration)
        elif activity == "rest" and duration >= 60:
            boundary_intent = RestIntent(duration_ticks=duration)
        else:
            boundary_intent = WaitIntent(duration_ticks=duration)
        boundary_context = RuleContext(
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=slice_command_id,
            actor_id=plan.actor_id,
            ruleset_version=context.ruleset_version or RULES_VERSION,
            simulation_command_id=context.command_id,
            travelling_window=activity not in {"rest", "eat", "drink", "wait"},
            continuous_training=plan.activity == "study" and activity == "study",
            explicit_training_window=(plan.started_tick, plan.target_tick)
            if activity == "study" and plan.stationary_only and plan.started_tick is not None else None,
        )
        background_choices = self.agent_policy.choose_background(
            state,
            exclude_actor_ids=frozenset({plan.actor_id}),
        )
        world_action = getattr(self.rule_engine, "evaluate_world_action", None)
        capture_transit_projection = self._eligible_itinerary_transit_projection(
            state, plan, context, activity=activity, reason=reason,
            reference=reference, subject=subject,
        )
        if plan.stationary_only and activity == "rest" and plan.started_tick is not None:
            resolution = self.rule_engine.evaluate_rest_window(
                state, started_tick=plan.started_tick, duration=duration, context=boundary_context, choices=background_choices,
            )
        elif capture_transit_projection:
            with self.rule_engine._capture_validated_projection():
                resolution = (
                    world_action(state, boundary_intent, boundary_context, background_choices)
                    if callable(world_action)
                    else self.rule_engine.evaluate(state, boundary_intent, boundary_context)
                )
        else:
            resolution = (
                world_action(state, boundary_intent, boundary_context, background_choices)
                if callable(world_action)
                else self.rule_engine.evaluate(state, boundary_intent, boundary_context)
            )
        events = list(resolution.events)
        if work_job_id is not None and events and isinstance(events[0], TimeAdvancedEvent):
            events[0] = events[0].model_copy(update={"work_job_id": work_job_id})
        combined_outcome = resolution.outcome
        scheduled = next((item for item in state.scheduled_events if item.id == reference), None) if reference else None
        actual_tick = state.current_tick + resolution.elapsed_ticks
        deferred_lives = scheduled is not None and actual_tick == tick and any(
            isinstance(event, LifeEndedEvent) for event in events
        )
        if deferred_lives:
            events = [event for event in events if not isinstance(event, LifeEndedEvent)]
            if resolution.outcome.code == "lifespan_expired":
                combined_outcome = RuleOutcome(
                    status="succeeded",
                    code="scheduled_event_boundary",
                    message="The earlier scheduled phase must resolve before lifespan closures at this tick.",
                    details={"scheduled_event_id": str(scheduled.id)},
                )
        expired_ids = [event.entity_id for event in events if isinstance(event, LifeEndedEvent)]
        if expired_ids:
            combined_outcome = RuleOutcome(
                status="partial",
                code="lifespan_expired",
                message="One or more characters reached their typed lifespan boundary.",
                details={"expired_character_ids": [str(value) for value in expired_ids]},
            )
        combined = RuleResolution(
            events=events,
            outcome=combined_outcome,
            elapsed_ticks=resolution.elapsed_ticks,
            ruleset_version=resolution.ruleset_version,
        )
        self._validate_rule_batch(state, combined)
        actual_tick = state.current_tick + combined.elapsed_ticks
        continuation = self._continuation(plan, state.current_tick)
        if (
            combined.outcome.code == "survival_pressure"
            and scheduled is not None
            and scheduled.handler_id == TRAVEL_RESOLUTION_HANDLER_ID
            and actual_tick == scheduled.due_tick
            and plan.activity == "travel"
        ):
            return self._resolved(
                state, context, combined, outcome="advanced", boundary=reason,
                interruption=None, continuation=continuation,
            )
        if combined.outcome.code in {"died_of_deprivation", "survival_pressure"}:
            return self._resolved(
                state, context, combined, outcome="stopped", boundary=combined.outcome.code,
                interruption=Interruption(
                    kind="boundary", tick=actual_tick, reason_code=combined.outcome.code, subject_id=plan.actor_id
                ),
            )
        if plan.actor_id in expired_ids:
            return self._resolved(
                state, context, combined, outcome="stopped", boundary="actor_lifespan_expired",
                interruption=Interruption(
                    kind="boundary", tick=actual_tick, reason_code="actor_lifespan_expired", subject_id=plan.actor_id
                ),
            )
        if expired_ids:
            return self._resolved(
                state, context, combined, outcome="advanced", boundary="npc_lifespan_expired",
                interruption=None, continuation=continuation,
            )
        if combined.outcome.status in {"rejected", "failed"}:
            return self._resolved(
                state, context, combined, outcome="rule_required", boundary=combined.outcome.code,
                interruption=Interruption(
                    kind="rule_required", tick=actual_tick,
                    reason_code=combined.outcome.code, subject_id=plan.actor_id, reference_id=reference,
                ),
            )
        if actual_tick < tick:
            return self._resolved(
                state, context, combined, outcome="stopped", boundary="rule_window_ended_early",
                interruption=Interruption(
                    kind="boundary", tick=actual_tick, reason_code="rule_window_ended_early", subject_id=plan.actor_id
                ),
            )
        if reason == "npc_plan_due" or scheduled is not None and (
            scheduled.phase == "decision" or self._scheduled_handler_supported(scheduled)
        ):
            return self._resolved(
                state, context, combined, outcome="advanced", boundary=reason,
                interruption=None, continuation=continuation,
            )
        if reason in {
            "background_deprivation_boundary", "background_self_care_boundary", "travel_in_progress",
            "routine_work_shift_start", "routine_work_rest_start", "stop_condition_boundary",
            "itinerary_auto_care_threshold", "autonomous_vacancy_year_boundary",
            "npc_meeting_start", "npc_routine_boundary", "npc_work_boundary", "work_attendance_boundary",
            "stationary_action_window",
        }:
            result = self._resolved(
                state, context, combined, outcome="advanced", boundary=reason,
                interruption=None, continuation=continuation,
            )
            if (capture_transit_projection
                and reason in {"travel_in_progress", "itinerary_auto_care_threshold"}
                and actual_tick == tick and not expired_ids):
                receipt = self._issue_itinerary_transit_projection_receipt(
                    state, context, plan, resolution, combined,
                    exact_events=tuple(events), boundary_tick=actual_tick,
                    reference=reference, subject=subject,
                )
                if receipt is not None:
                    result._itinerary_transit_projection_receipt = receipt
            return result
        return self._resolved(
            state, context, combined, outcome="rule_required", boundary=reason,
            interruption=Interruption(
                kind="rule_required", tick=actual_tick, reason_code=reason,
                subject_id=subject, reference_id=reference,
            ),
            continuation=continuation,
        )

    def _decision_gate(
        self,
        state: WorldState,
        context: SimulationContext,
        request: DecisionRequest,
        *,
        continuation: SimulationPlan | None = None,
    ) -> SimulationResolution:
        response = self._matching_response(context, request)
        if request.kind == "npc.plan_review" and response is None:
            actor = state.entities.get(request.actor_id)
            skip_reason = None
            if actor is None or actor.status != "active" or request.actor_id == state.player_id:
                skip_reason = "npc_review_actor_unavailable"
            elif request.actor_id in context.externally_controlled_actor_ids:
                skip_reason = "npc_review_actor_user_controlled"
            elif not any(goal.owner_id == request.actor_id and goal.status == "active"
                         and (goal.deadline is None or goal.deadline >= state.current_tick)
                         for goal in state.goals):
                skip_reason = "npc_review_no_pursuable_goal"
            elif any(plan.actor_id == request.actor_id and plan.status in {"ready", "running"}
                     for plan in state.npc_plans):
                skip_reason = "npc_review_existing_plan_in_progress"
            if skip_reason is not None:
                return self._resolve_npc_plan_review(
                    state, context, request, None, continuation, policy_reason=skip_reason,
                )
        if response is None:
            return self._empty(
                state,
                context,
                outcome="decision_required",
                reason=request.reason_code,
                interruption=Interruption(kind="decision_required", tick=request.at_tick, reason_code=request.reason_code, subject_id=request.actor_id, reference_id=request.request_id),
                decision_requests=(request,),
            )
        valid_options = {option.option_id for option in request.options}
        if response.option_id not in valid_options:
            raise StaleDecisionResponse("Decision response option does not belong to its bound request.")
        policy_planning = request.kind == "npc.plan_review" and response.source == "policy"
        if response.source != request.decision_owner and not policy_planning:
            raise StaleDecisionResponse("Decision response source does not match the request's decision owner.")
        if request.kind == "npc.plan_review":
            return self._resolve_npc_plan_review(state, context, request, response, continuation)
        if request.kind == "organization.succession":
            return self._resolve_succession_decision(state, context, request, response, continuation)
        return self._empty(
            state,
            context,
            outcome="rule_required",
            reason="accepted_model_decision_requires_rule_resolution",
            interruption=Interruption(kind="rule_required", tick=request.at_tick, reason_code="accepted_model_decision_requires_rule_resolution", subject_id=request.actor_id, reference_id=request.request_id),
            accepted_decisions=(response,),
        )

    def _resolve_npc_plan_review(self, state, context, request, response, continuation, *, policy_reason=None):
        from world_engine.domain.npc_plan_events import NpcPlanCreatedEvent, NpcPlanRevisedEvent, npc_plan_event_id
        from world_engine.domain.npc_plan_review import npc_plan_review_request_id
        from world_engine.domain.npc_plan_review_events import NpcPlanReviewResolvedEvent, npc_plan_review_event_id
        from world_engine.domain.reducers import apply_event, state_hash

        scheduled = next((item for item in state.scheduled_events if item.id == request.causal_subject_id), None)
        if scheduled is None or request.request_id != npc_plan_review_request_id(context.command_id, context.ruleset_version, scheduled):
            raise StaleDecisionResponse("The NPC review occurrence no longer matches its request.")
        events = []
        if response is None:
            outcome = "defer" if policy_reason == "npc_review_existing_plan_in_progress" else "skipped"
            source, call_id, proposed = "policy", None, None
            reason = policy_reason or "npc_review_skipped"
        else:
            if response.npc_planning_status is None or response.npc_plan_state_hash != state_hash(state):
                raise StaleDecisionResponse("NPC plan proposal does not match the reviewed world snapshot.")
            outcome, source, call_id = response.npc_planning_status, response.source, response.npc_model_call_id
            proposed = response.npc_plan
            reason = response.npc_planning_rejection or f"npc_planning_{outcome}"
            if proposed is not None:
                if proposed.actor_id != request.actor_id or proposed.created_tick != state.current_tick:
                    raise StaleDecisionResponse("NPC plan proposal names a different actor or starting tick.")
                from .npc_capabilities import build_default_npc_capability_registry, preflight_npc_plan_step
                from world_engine.domain.npc_plan_review import npc_planning_preflight_state
                registry = self.npc_capability_registry or build_default_npc_capability_registry()
                for step in proposed.steps:
                    capability = registry.get(step.capability_id)
                    if capability is None:
                        raise StaleDecisionResponse("NPC plan refers to an unregistered capability.")
                    capability.intent_model.model_validate(step.arguments)
                preflight = preflight_npc_plan_step(
                    npc_planning_preflight_state(state, context.world_id), proposed.actor_id, proposed.steps[0], RuleContext(
                        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
                        actor_id=proposed.actor_id, ruleset_version=context.ruleset_version,
                    ), registry=registry, rule_engine=self.rule_engine,
                    externally_controlled_actor_ids=context.externally_controlled_actor_ids,
                )
                if preflight.status == "rejected":
                    raise StaleDecisionResponse("The proposed NPC plan's first step is no longer feasible.")
                for old in state.npc_plans:
                    if old.actor_id == request.actor_id and old.status == "blocked":
                        cancelled = old.model_copy(update={
                            "revision": old.revision + 1, "status": "cancelled", "updated_tick": state.current_tick,
                            "next_review_tick": None, "active_execution": None,
                            "reason": "Replaced by a newly reviewed NPC plan.",
                        })
                        events.append(NpcPlanRevisedEvent(
                            event_id=npc_plan_event_id(context.command_id, context.ruleset_version, "revised", old.plan_id, cancelled.revision),
                            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
                            actor_id=old.actor_id, ruleset_version=context.ruleset_version,
                            expected_revision=old.revision, plan=cancelled,
                        ))
                events.append(NpcPlanCreatedEvent(
                    event_id=npc_plan_event_id(context.command_id, context.ruleset_version, "created", proposed.plan_id, 1),
                    world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
                    actor_id=proposed.actor_id, ruleset_version=context.ruleset_version, plan=proposed,
                ))
        events.append(NpcPlanReviewResolvedEvent(
            event_id=npc_plan_review_event_id(context.command_id, context.ruleset_version, scheduled.id, scheduled.revision),
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            ruleset_version=context.ruleset_version, scheduled_event_id=scheduled.id,
            expected_schedule_revision=scheduled.revision, actor_id=request.actor_id,
            at_tick=state.current_tick, decision_request_id=request.request_id,
            based_on_state_version=context.state_version, source=source, outcome=outcome,
            plan_id=proposed.plan_id if proposed is not None else None,
            promise_choice=response.promise_choice if response is not None else None,
            model_call_id=str(call_id) if call_id is not None else None,
            reason=reason,
        ))
        scratch = state
        for event in events:
            scratch = apply_event(scratch, event)
        resolution = RuleResolution(
            events=events, elapsed_ticks=0, ruleset_version=context.ruleset_version,
            outcome=RuleOutcome(status="succeeded", code="npc_plan_review_resolved", message="An NPC planning review was recorded."),
        )
        return self._resolved(
            state, context, resolution, outcome="advanced" if continuation is not None else "completed",
            boundary="npc_plan_review_resolved", interruption=None, continuation=continuation,
            accepted_decisions=(response,) if response is not None else (),
        )

    def _resolve_succession_decision(
        self,
        state: WorldState,
        context: SimulationContext,
        request: DecisionRequest,
        response: DecisionResponse,
        continuation: SimulationPlan | None,
    ) -> SimulationResolution:
        target = request.resolution_target
        if target is None or request.causal_subject_id != target.predecessor_id:
            return self._empty(
                state,
                context,
                outcome="rule_required",
                reason="succession_target_unavailable",
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick,
                    reason_code="succession_target_unavailable", reference_id=request.request_id,
                ),
            )
        source_death_event_id = context.causal_death_event_ids.get(target.predecessor_id)
        if source_death_event_id is None:
            return self._empty(
                state,
                context,
                outcome="rule_required",
                reason="succession_cause_event_unavailable",
                interruption=Interruption(
                    kind="rule_required", tick=state.current_tick,
                    reason_code="succession_cause_event_unavailable", reference_id=request.request_id,
                ),
            )

        successor_id: UUID | None
        if response.option_id == "leave_vacant":
            successor_id = None
            event_option_id = "leave_vacant"
        else:
            prefix = "leader_candidate_" if target.slot == "leader" else f"office_{target.office_id}_candidate_"
            if not response.option_id.startswith(prefix):
                raise StaleDecisionResponse("Succession option does not belong to the recorded slot.")
            try:
                candidate_index = int(response.option_id[len(prefix):]) - 1
            except ValueError as exc:
                raise StaleDecisionResponse("Succession option has an invalid candidate index.") from exc
            if not 0 <= candidate_index < len(target.candidate_ids):
                raise StaleDecisionResponse("Succession option candidate is no longer eligible.")
            successor_id = target.candidate_ids[candidate_index]
            successor = state.entities.get(successor_id)
            if successor is None or successor.status in {"dead", "retired"}:
                raise StaleDecisionResponse("Succession candidate is no longer active.")
            event_option_id = (
                f"leader_holder_{successor_id.hex}"
                if target.slot == "leader"
                else f"office_{target.office_id}_holder_{successor_id.hex}"
            )

        linked_offices = None
        if target.slot == "leader":
            governance = GovernanceComponent.model_validate(state.entities[target.organization_id].components["governance"])
            linked_offices = leadership_office_ids_held_by(governance.offices, target.predecessor_id)
        event = GovernanceSuccessionEvent(
            event_id=self._event_id(
                context, state.current_tick,
                f"governance-succession:{request.request_id}:{response.option_id}",
            ),
            organization_id=target.organization_id,
            slot=target.slot,
            office_id=target.office_id,
            predecessor_id=target.predecessor_id,
            successor_id=successor_id,
            expected_governance_revision=target.expected_governance_revision,
            decision_basis=target.decision_basis,
            decision_request_id=request.request_id,
            decision_option_id=event_option_id,
            source_death_event_id=source_death_event_id,
            evidence_refs=[
                source_death_event_id,
                request.request_id,
                target.organization_id,
                target.predecessor_id,
            ],
            rationale=response.explanation,
            linked_leadership_office_ids=linked_offices,
        )
        resolution = RuleResolution(
            events=[event],
            outcome=RuleOutcome(
                status="succeeded",
                code="succession_resolved",
                message="The policy-bound leadership choice was recorded.",
                details={"organization_id": str(target.organization_id), "slot": target.slot},
            ),
            elapsed_ticks=0,
            ruleset_version=context.ruleset_version,
        )
        # This decision event is built here rather than by RuleEngine, so apply
        # it once before returning the proposal. Storage validates it on commit.
        apply_event(state, event)
        return self._resolved(
            state,
            context,
            resolution,
            outcome="advanced" if continuation is not None else "completed",
            boundary="succession_resolved",
            interruption=None,
            continuation=self._continuation(continuation, state.current_tick) if continuation is not None else None,
            accepted_decisions=(response,),
        )

    @staticmethod
    def _matching_response(context: SimulationContext, request: DecisionRequest) -> DecisionResponse | None:
        if not context.decision_responses:
            return None
        if len(context.decision_responses) != 1:
            raise StaleDecisionResponse("Only one fresh response may resolve a simulation decision boundary.")
        response = context.decision_responses[0]
        if response.state_version != context.state_version or response.at_tick != request.at_tick or response.request_id != request.request_id:
            raise StaleDecisionResponse("Decision response was generated for a different request, state version, or tick.")
        return response

    def _reject_unconsumed_decisions(self, context: SimulationContext) -> None:
        if context.decision_responses:
            raise StaleDecisionResponse("No pending decision request matches the supplied response.")

    def _goal_conflict_request(self, state, context, actor_id, goal_ids):
        goals = {goal.id: goal for goal in state.goals}
        return DecisionRequest(
            request_id=self._event_id(context, state.current_tick, f"goal-conflict:{actor_id}"),
            kind="npc.goal_conflict",
            actor_id=actor_id,
            at_tick=state.current_tick,
            state_version=context.state_version,
            title="Choose which equally important goal to pursue",
            reason_code="equal_top_priority_goals",
            options=tuple(
                DecisionOption(option_id=f"goal_{goal_id.hex}", label=goals[goal_id].title)
                for goal_id in goal_ids if goal_id in goals
            ),
            evidence_ids=goal_ids,
        )

    def _scheduled_decision_request(self, state, context, scheduled):
        if scheduled.handler_id == "npc.goal_review":
            from world_engine.domain.npc_plan_review import (
                npc_plan_review_request_id, validate_npc_plan_review_schedule,
            )
            actor_id = validate_npc_plan_review_schedule(state, scheduled, context.world_id, require_eligible=False)
            return DecisionRequest(
                request_id=npc_plan_review_request_id(context.command_id, context.ruleset_version, scheduled),
                kind="npc.plan_review", actor_id=actor_id, causal_subject_id=scheduled.id,
                at_tick=state.current_tick, state_version=context.state_version,
                title="Review an NPC's next plan", reason_code="npc_plan_review_due",
                options=(DecisionOption(option_id="plan", label="Propose the NPC's next plan"),),
                evidence_ids=(scheduled.id,),
            )
        payload = scheduled.payload
        raw_options = payload.get("decision_options", []) if isinstance(payload, dict) else []
        options: list[DecisionOption] = []
        if isinstance(raw_options, list):
            for item in raw_options[:64]:
                if not isinstance(item, dict):
                    continue
                try:
                    options.append(DecisionOption.model_validate(item))
                except Exception:
                    continue
        actor_raw = payload.get("actor_id") if isinstance(payload, dict) else None
        try:
            actor_id = UUID(str(actor_raw)) if actor_raw is not None else None
        except ValueError:
            actor_id = None
        title = payload.get("title") if isinstance(payload, dict) else None
        return DecisionRequest(
            request_id=self._event_id(context, state.current_tick, f"scheduled-decision:{scheduled.id}"),
            kind="scheduled." + scheduled.handler_id.replace("_", "."),
            actor_id=actor_id,
            at_tick=state.current_tick,
            state_version=context.state_version,
            title=title if isinstance(title, str) and title.strip() else f"Resolve scheduled decision: {scheduled.handler_id}",
            reason_code="scheduled_decision_due",
            options=tuple(options),
            evidence_ids=(scheduled.id,),
        )

    def _food_shortage_request(self, state, context, actor_id):
        return DecisionRequest(
            request_id=self._event_id(context, state.current_tick, f"food-shortage:{actor_id}"),
            kind="npc.survival.food_shortage",
            actor_id=actor_id,
            at_tick=state.current_tick,
            state_version=context.state_version,
            title="Food need is critical and no owned food stock is available",
            reason_code="critical_food_need_without_inventory",
            options=(
                DecisionOption(option_id="seek_food", label="Seek a lawful food source"),
                DecisionOption(option_id="request_help", label="Ask a known contact for help"),
                DecisionOption(option_id="reduce_activity", label="Stop and conserve energy"),
            ),
        )

    def _food_resource(self, state: WorldState) -> str | None:
        roles = state.metadata.get("resource_roles")
        return str(roles.get("food_resource_id")) if isinstance(roles, dict) and roles.get("food_resource_id") else None

    @staticmethod
    def _water_resource(state: WorldState) -> str | None:
        roles = state.metadata.get("resource_roles")
        return str(roles.get("water_resource_id")) if isinstance(roles, dict) and roles.get("water_resource_id") else None

    @staticmethod
    def _food_stock_accounts(state: WorldState, actor_id: UUID, resource_id: str | None) -> tuple[UUID, ...]:
        if resource_id is None:
            return ()
        actor = state.entities[actor_id]
        raw_inventory = actor.components.get("inventory")
        if not isinstance(raw_inventory, dict):
            return ()
        from world_engine.domain.components import InventoryComponent

        inventory = InventoryComponent.model_validate(raw_inventory)
        account_ids: list[UUID] = []
        for lot in inventory.lots:
            account = state.resources.get(lot.account_id)
            if (
                lot.resource_id == resource_id
                and account is not None
                and account.owner_id == actor_id
                and account.resource_id == resource_id
                and account.balance > 0
                and (lot.expiry_tick is None or lot.expiry_tick > state.current_tick)
            ):
                account_ids.append(lot.account_id)
        return tuple(sorted(set(account_ids), key=str))

    def _stop_boundary(self, state: WorldState, condition: StopCondition, activity: str, plan, duration: int | None) -> int | None:
        if condition.kind not in {"need_at_or_above", "need_at_or_below"} or duration is None:
            return None
        if condition.subject_id != plan.actor_id:
            return None
        if activity != "rest" or condition.need != "rest" or condition.kind != "need_at_or_above":
            return None
        entity = state.entities[plan.actor_id]
        raw = entity.components.get("needs")
        if not isinstance(raw, dict) or raw.get("rest") is None:
            return None
        value = int(raw["rest"]["value"])
        if value >= int(condition.threshold or 0):
            return state.current_tick
        # RuleEngine's registered rest rule recovers 36 points per world day.
        ticks_needed = ((int(condition.threshold) - value) * state.calendar.day_seconds + 35) // 36
        return state.current_tick + max(60, ticks_needed)

    def _validate_rule_batch(self, state: WorldState, resolution: RuleResolution) -> None:
        """Check the clock contract without replaying native validated batches.

        RuleEngine._resolution validates its complete batch against the typed
        reducers before returning. Replaying it here built the same complete
        WorldState repeatedly in the profiled 453-entity path. Injected rule
        evaluators remain untrusted and continue through a full reducer replay.
        """
        events = tuple(parse_event(event) for event in resolution.events)
        if self._native_rule_engine:
            time_events = [event for event in events if isinstance(event, TimeAdvancedEvent)]
            if resolution.elapsed_ticks:
                delay = bool(events and isinstance(events[0], TravelJourneyChangedEvent) and events[0].operation == "delay")
                if len(time_events) != 1 or len(events) <= int(delay) or events[int(delay)] != time_events[0]:
                    raise RuntimeError("RuleEngine timed batches require one clock advance after any travel delay.")
                if time_events[0].to_tick != state.current_tick + resolution.elapsed_ticks:
                    raise RuntimeError("RuleEngine event batch time does not match elapsed_ticks.")
            elif time_events:
                raise RuntimeError("RuleEngine zero-duration batch cannot contain TimeAdvancedEvent.")
            return

        projected = state
        for event in events:
            projected = apply_event(projected, event)
        if projected.current_tick != state.current_tick + resolution.elapsed_ticks:
            raise RuntimeError("RuleEngine event batch time does not match elapsed_ticks.")

    @staticmethod
    def _stop_satisfied_after(state: WorldState, events: tuple[DomainEvent, ...], condition: StopCondition) -> bool:
        """Evaluate stop conditions from typed events without another full projection."""
        from world_engine.domain.gameplay_events import (
            ComponentReplacedEvent,
            DamageAppliedEvent,
            GoalCreatedEvent,
            GoalUpdatedEvent,
            HealthRestoredEvent,
        )
        from world_engine.domain.models import EntityFieldChangedEvent

        if condition.kind in {"need_at_or_below", "need_at_or_above"}:
            entity = state.entities.get(condition.subject_id)
            raw = entity.components.get("needs") if entity is not None else None
            need_value = raw.get(condition.need) if isinstance(raw, dict) else None
            if not isinstance(need_value, dict):
                return False
            value = int(need_value["value"])
            for event in events:
                if isinstance(event, ComponentReplacedEvent) and event.entity_id == condition.subject_id and event.component_name == "needs":
                    replacement = event.replacement.get(condition.need)
                    if isinstance(replacement, dict):
                        value = int(replacement["value"])
            return value <= condition.threshold if condition.kind == "need_at_or_below" else value >= condition.threshold

        if condition.kind == "entity_status":
            entity = state.entities.get(condition.subject_id)
            if entity is None:
                return False
            status = entity.status
            health = entity.components.get("health")
            health_current = int(health["current"]) if isinstance(health, dict) else None
            for event in events:
                if isinstance(event, EntityFieldChangedEvent) and event.entity_id == condition.subject_id and event.field_name == "status":
                    status = event.value
                elif isinstance(event, LifeEndedEvent) and event.entity_id == condition.subject_id:
                    status = "dead"
                elif isinstance(event, DamageAppliedEvent) and event.target_id == condition.subject_id and health_current is not None:
                    health_current -= event.damage
                    if health_current <= 0:
                        status = "dead"
                elif isinstance(event, HealthRestoredEvent) and event.target_id == condition.subject_id and health_current is not None:
                    health_current += event.amount
            return status == condition.status

        if condition.kind == "goal_completed":
            goal = next((item for item in state.goals if item.id == condition.goal_id), None)
            status = goal.status if goal is not None else None
            for event in events:
                if isinstance(event, GoalCreatedEvent) and event.goal.id == condition.goal_id:
                    status = event.goal.status
                elif isinstance(event, GoalUpdatedEvent) and event.goal_id == condition.goal_id and event.status is not None:
                    status = event.status
            return status == "succeeded"
        return False

    def _resolved(
        self,
        state,
        context,
        resolution: RuleResolution,
        *,
        outcome: str,
        boundary: str,
        interruption: Interruption | None,
        continuation: SimulationPlan | None = None,
        accepted_decisions: tuple[DecisionResponse, ...] = (),
        work_purchase_affordability: WorkPurchaseAffordabilityReceipt | None = None,
    ) -> SimulationResolution:
        tick = state.current_tick + resolution.elapsed_ticks
        events = tuple(parse_event(event) for event in resolution.events)
        return SimulationResolution(
            outcome=outcome,  # type: ignore[arg-type]
            events=events,
            next_tick=tick,
            interruption=interruption,
            accepted_decisions=accepted_decisions,
            continuation=continuation,
            rule_resolution=resolution,
            metrics=ResolutionMetrics(
                from_tick=state.current_tick,
                to_tick=tick,
                ticks_advanced=resolution.elapsed_ticks,
                integration_boundary=boundary,
                event_count=len(events),
                depth=context.depth,
                speed=context.speed,
                needs_changed=tuple(self._needs_changed(events)),
                resources_consumed=tuple(self._resource_consumption(events)),
                work_purchase_affordability=work_purchase_affordability,
            ),
        )

    @staticmethod
    def _needs_changed(events: tuple[DomainEvent, ...]) -> list[str]:
        from world_engine.domain.gameplay_events import ComponentReplacedEvent

        return [event.component_name for event in events if isinstance(event, ComponentReplacedEvent) and event.component_name == "needs"]

    @staticmethod
    def _resource_consumption(events: tuple[DomainEvent, ...]) -> list[tuple[str, str]]:
        from world_engine.domain.gameplay_events import ResourceConsumedEvent, ResourceProducedEvent

        flows: list[tuple[str, str]] = []
        for event in events:
            if isinstance(event, ResourceConsumedEvent):
                flows.extend((flow.resource_id, str(flow.quantity)) for flow in event.flows)
            elif isinstance(event, ResourceProducedEvent):
                flows.extend((flow.resource_id, str(flow.quantity)) for flow in event.consumed_inputs)
        return flows

    def _empty(
        self,
        state: WorldState,
        context: SimulationContext,
        *,
        outcome: str,
        reason: str,
        interruption: Interruption | None = None,
        decision_requests: tuple[DecisionRequest, ...] = (),
        accepted_decisions: tuple[DecisionResponse, ...] = (),
        work_purchase_affordability: WorkPurchaseAffordabilityReceipt | None = None,
    ) -> SimulationResolution:
        tick = state.current_tick
        return SimulationResolution(
            outcome=outcome,  # type: ignore[arg-type]
            events=(),
            next_tick=tick,
            interruption=interruption,
            decision_requests=decision_requests,
            accepted_decisions=accepted_decisions,
            metrics=ResolutionMetrics(
                from_tick=tick,
                to_tick=tick,
                ticks_advanced=0,
                integration_boundary=reason,
                event_count=0,
                depth=context.depth,
                speed=context.speed,
                work_purchase_affordability=work_purchase_affordability,
            ),
        )

    @staticmethod
    def _event_id(context: SimulationContext, tick: int, key: str) -> UUID:
        return uuid5(context.command_id, f"{context.world_seed}:{context.state_version}:{tick}:{key}")

    @staticmethod
    def _stop_satisfied(state: WorldState, conditions: tuple[StopCondition, ...]) -> bool:
        for condition in conditions:
            if condition.kind in {"need_at_or_below", "need_at_or_above"}:
                entity = state.entities.get(condition.subject_id)
                raw = entity.components.get("needs") if entity is not None else None
                if not isinstance(raw, dict) or raw.get(condition.need) is None:
                    continue
                value = int(raw[condition.need]["value"])
                if condition.kind == "need_at_or_below" and value <= int(condition.threshold):
                    return True
                if condition.kind == "need_at_or_above" and value >= int(condition.threshold):
                    return True
            elif condition.kind == "entity_status":
                entity = state.entities.get(condition.subject_id)
                if entity is not None and entity.status == condition.status:
                    return True
            elif condition.kind == "goal_completed":
                if any(goal.id == condition.goal_id and goal.status == "succeeded" for goal in state.goals):
                    return True
        return False

_PHASE_ORDER = {
    "environment": 0,
    "delivery": 1,
    "decision": 2,
    "resolution": 3,
    "observation": 4,
    "maintenance": 5,
}
