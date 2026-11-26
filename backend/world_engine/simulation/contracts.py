"""Public immutable contracts for deterministic, event-bounded simulation."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, StrictInt, model_validator
from world_engine.domain.components import ExactDecimal, ResourceName
from world_engine.rules.contracts import ConsumeIntent, ExploreIntent, RuleResolution

from world_engine.domain.models import DomainEvent
from world_engine.domain.npc_plan_review import NpcPlanningPolicyReason, NpcPlanningRejection
from world_engine.domain.npc_plans import NPC_EXPLORATION_TICKS, NpcPlan
from world_engine.domain.relationship_promises import PromiseReviewChoice
from world_engine.domain.travel_itinerary import TravelItinerary
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


NeedName = Literal["food", "water", "rest", "shelter", "safety", "health", "social", "belonging"]
ActivityName = Literal["wait", "rest", "eat", "drink", "travel", "work", "study", "produce", "socialize"]
PlanConstraint = Literal["do_not_eat", "do_not_drink", "do_not_rest"]


class StopCondition(FrozenModel):
    kind: Literal["need_at_or_below", "need_at_or_above", "entity_status", "goal_completed"]
    subject_id: UUID | None = None
    need: NeedName | None = None
    threshold: int | None = Field(default=None, ge=0, le=100)
    status: str | None = Field(default=None, min_length=1, max_length=64)
    goal_id: UUID | None = None

    @model_validator(mode="after")
    def condition_shape(self) -> StopCondition:
        if self.kind in {"need_at_or_below", "need_at_or_above"}:
            if self.subject_id is None or self.need is None or self.threshold is None:
                raise ValueError("need stop conditions require subject_id, need, and threshold")
        elif self.kind == "entity_status" and (self.subject_id is None or self.status is None):
            raise ValueError("entity_status stop conditions require subject_id and status")
        elif self.kind == "goal_completed" and self.goal_id is None:
            raise ValueError("goal_completed stop conditions require goal_id")
        return self


class WorkPurchaseTarget(FrozenModel):
    """Named listing and exact quantity a recurring work job is saving for."""

    market_id: UUID
    listing_id: UUID
    resource_id: ResourceName
    currency_resource_id: ResourceName
    seller_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class RoutineWorkSchedule(FrozenModel):
    """Explicit recurring paid-shift schedule bound to one active assignment.

    ``first_shift_tick`` is an absolute world tick. Later shifts begin at the
    same time of day every ``repeat_every_days`` calendar days. The wage and
    wage account are deliberately resolved from the registered assignment and
    posting at execution time rather than being caller-selected values.
    """

    assignment_id: UUID
    first_shift_tick: StrictInt = Field(ge=-(2**63), le=2**63 - 1)
    repeat_every_days: StrictInt = Field(gt=0, le=2**31 - 1)
    balance_target: ExactDecimal | None = Field(default=None, ge=Decimal("0"), exclude_if=lambda value: value is None)
    purchase_target: WorkPurchaseTarget | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def only_one_savings_target(self) -> RoutineWorkSchedule:
        if self.balance_target is not None and self.purchase_target is not None:
            raise ValueError("routine work cannot set both a balance_target and purchase_target")
        return self


class LongActionPlan(FrozenModel):
    """A bounded player/NPC action request. It is not itself a world event."""

    kind: Literal["long_action"] = "long_action"
    actor_id: UUID
    target_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    activity: ActivityName | Literal["consume", "explore"]
    constraints: tuple[PlanConstraint, ...] = Field(default=(), max_length=64)
    stop_conditions: tuple[StopCondition, ...] = Field(default=(), max_length=256)
    route_id: UUID | None = None
    travel_itinerary: TravelItinerary | None = Field(default=None, exclude_if=lambda value: value is None)
    itinerary_auto_care: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    depart_immediately: bool = Field(default=False, exclude_if=lambda value: not value)
    skill_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    producer_id: UUID | None = None
    recipe_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    work_schedule: RoutineWorkSchedule | None = Field(default=None, exclude_if=lambda value: value is None)
    started_tick: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    consumption: ConsumeIntent | None = Field(default=None, exclude_if=lambda value: value is None)
    exploration: ExploreIntent | None = Field(default=None, exclude_if=lambda value: value is None)
    stationary_only: StrictBool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def action_has_parameters(self) -> LongActionPlan:
        if self.stationary_only and (self.activity not in {"wait", "rest", "study"} or self.started_tick is None
                                    or self.target_tick - self.started_tick < (60 if self.activity == "rest" else 1)):
            raise ValueError("An explicit stationary action requires a fixed wait, rest or study interval.")
        if self.activity == "consume":
            if (self.consumption is None or self.started_tick is None
                or self.target_tick - self.started_tick != self.consumption.duration_ticks
                or self.constraints or self.stop_conditions):
                raise ValueError("consumption jobs require one exact bounded intent with no adaptive policy")
        elif self.consumption is not None:
            raise ValueError("consumption is only valid for an exact consumption job")
        if self.activity == "explore":
            if (self.exploration is None or self.started_tick is None
                or self.target_tick - self.started_tick != NPC_EXPLORATION_TICKS
                or self.constraints or self.stop_conditions):
                raise ValueError("exploration requires one site and a fixed search interval")
        elif self.exploration is not None:
            raise ValueError("exploration is only valid for a search job")
        if self.activity == "travel" and (self.route_id is None) == (self.travel_itinerary is None):
            raise ValueError("travel actions require exactly one of route_id or travel_itinerary")
        if self.activity == "travel" and self.travel_itinerary is not None:
            if self.started_tick is None:
                raise ValueError("itinerary travel requires a fixed started_tick")
            if self.travel_itinerary.actor_id != self.actor_id:
                raise ValueError("the persisted itinerary actor must match the long action actor")
            if self.travel_itinerary.created_tick != self.started_tick:
                raise ValueError("the persisted itinerary start must match the long action started_tick")
            if self.travel_itinerary.deadline_tick != self.target_tick:
                raise ValueError("the persisted itinerary deadline must match the long action target_tick")
        if self.activity == "study" and self.skill_id is None:
            raise ValueError("study actions require a skill_id")
        if self.activity == "produce" and (self.producer_id is None or self.recipe_id is None):
            raise ValueError("produce actions require producer_id and recipe_id")
        if self.activity == "work" and self.work_schedule is None:
            raise ValueError("work actions require an explicit recurring work schedule")
        if self.activity != "work" and self.work_schedule is not None:
            raise ValueError("work_schedule is only valid for work actions")
        if (
            self.activity == "work"
            and self.started_tick is not None
            and self.work_schedule is not None
            and self.work_schedule.first_shift_tick < self.started_tick
        ):
            raise ValueError("the first work shift cannot precede the long action start")
        if self.activity != "travel" and self.route_id is not None:
            raise ValueError("route_id is only valid for travel actions")
        if self.activity != "travel" and self.travel_itinerary is not None:
            raise ValueError("travel_itinerary is only valid for travel actions")
        if self.itinerary_auto_care and self.travel_itinerary is None:
            raise ValueError("itinerary_auto_care requires a persisted travel_itinerary")
        if self.depart_immediately and self.activity != "travel":
            raise ValueError("immediate departure is only valid for travel actions")
        if self.activity != "study" and self.skill_id is not None:
            raise ValueError("skill_id is only valid for study actions")
        if self.activity != "produce" and (self.producer_id is not None or self.recipe_id is not None):
            raise ValueError("producer_id and recipe_id are only valid for produce actions")
        return self


class WorldAdvancePlan(FrozenModel):
    """Advance the world under ordinary agent policy, without a focal actor."""

    kind: Literal["world_advance"] = "world_advance"
    target_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    started_tick: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    stop_conditions: tuple[StopCondition, ...] = Field(default=(), max_length=256)


SimulationPlan: TypeAlias = LongActionPlan | WorldAdvancePlan


class DecisionOption(FrozenModel):
    option_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=240)
    effect_summary: str = Field(default="", max_length=1000)


class GovernanceDecisionTarget(FrozenModel):
    """Internal mapping from safe option IDs to a governance slot and candidates."""

    organization_id: UUID
    slot: Literal["leader", "office"]
    office_id: str | None = None
    predecessor_id: UUID
    decision_basis: Literal["election", "appointment", "inheritance", "council", "none"]
    expected_governance_revision: int = Field(ge=1)
    candidate_ids: tuple[UUID, ...] = Field(default=(), max_length=63)


class DecisionRequest(FrozenModel):
    request_id: UUID
    kind: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    decision_owner: Literal["model", "user"] = "model"
    causal_subject_id: UUID | None = Field(default=None, exclude=True)
    resolution_target: GovernanceDecisionTarget | None = Field(default=None, exclude=True)
    actor_id: UUID | None = None
    at_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    state_version: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=240)
    reason_code: str = Field(min_length=1, max_length=120)
    options: tuple[DecisionOption, ...] = Field(default=(), max_length=64)
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=1000)


class DecisionResponse(FrozenModel):
    """A model/user choice tied to exactly one request snapshot."""

    request_id: UUID
    state_version: int = Field(ge=0)
    at_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    source: Literal["model", "user", "policy"] = "model"
    option_id: str = Field(min_length=1, max_length=120)
    explanation: str = Field(default="", max_length=2000)
    npc_planning_status: Literal["ready", "defer", "invalid"] | None = Field(default=None, exclude_if=lambda value: value is None)
    npc_planning_rejection: NpcPlanningRejection | None = Field(default=None, exclude_if=lambda value: value is None)
    npc_planning_policy_reason: NpcPlanningPolicyReason | None = Field(default=None, exclude_if=lambda value: value is None)
    npc_plan: NpcPlan | None = Field(default=None, exclude_if=lambda value: value is None)
    npc_plan_state_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None)
    promise_choice: PromiseReviewChoice | None = Field(default=None, exclude_if=lambda value: value is None)
    npc_model_call_id: str | None = Field(default=None, min_length=1, max_length=128,
                                           pattern=r"^[a-zA-Z0-9_.:-]+$", exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def npc_planning_response_is_complete(self):
        if self.promise_choice is not None and self.npc_planning_status != "defer":
            raise ValueError("A promise choice requires a typed NPC deferral.")
        if self.npc_planning_rejection is not None and self.npc_planning_status != "invalid":
            raise ValueError("Only invalid NPC planning responses can contain a rejection code.")
        if self.npc_planning_policy_reason is not None and (
            self.npc_planning_status != "defer" or self.source != "policy"
        ):
            raise ValueError("NPC planning policy reasons require a policy-sourced defer.")
        if self.npc_planning_status is None:
            if any(value is not None for value in (
                self.npc_plan, self.npc_plan_state_hash, self.npc_model_call_id,
                self.npc_planning_policy_reason,
            )):
                raise ValueError("NPC planning fields require a typed planning status.")
            if self.source == "policy":
                raise ValueError("Policy responses are reserved for typed NPC planning reviews.")
            return self
        if self.option_id != "plan" or self.source == "user" or self.npc_plan_state_hash is None:
            raise ValueError("NPC planning responses require a snapshot-bound planning option.")
        if (self.npc_planning_status == "ready") != (self.npc_plan is not None):
            raise ValueError("Only a ready NPC planning response can contain a plan.")
        if (self.source == "model") != (self.npc_model_call_id is not None):
            raise ValueError("Model planning responses require their actual call receipt.")
        return self


class SimulationContext(FrozenModel):
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    command_actor_id: UUID | None = None
    externally_controlled_actor_ids: frozenset[UUID] = frozenset()
    allow_autonomous_vacancy: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    allow_autonomous_meeting_attendance: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    allow_npc_routine_mobility: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    work_job_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    world_seed: str = Field(min_length=1, max_length=256)
    state_version: int = Field(ge=0)
    ruleset_version: str = Field(default="1.0.0", min_length=1, max_length=100)
    depth: Literal["standard", "deep", "exhaustive"] = "deep"
    speed: Literal["fast", "balanced", "patient"] = "balanced"
    max_step_ticks: int = Field(default=31_622_400, ge=60, le=2**63 - 1)
    max_steady_macro_days: StrictInt | None = Field(
        default=None, ge=1, le=2**31 - 1, exclude_if=lambda value: value is None,
    )
    decision_responses: tuple[DecisionResponse, ...] = Field(default=(), max_length=64)
    causal_death_event_ids: dict[UUID, UUID] = Field(default_factory=dict, max_length=1000)
    journey_resolution_receipts: dict[UUID, TravelJourneyChangedEvent] = Field(default_factory=dict, max_length=1000)
    journey_receipt_branch_ids: frozenset[UUID] = frozenset()
    itinerary_journey_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    itinerary_auto_care: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    _itinerary_transit_projection_binding: tuple[UUID, str, int, str] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def itinerary_auto_care_has_verified_journey(self) -> SimulationContext:
        if self.itinerary_auto_care and self.itinerary_journey_id is None:
            raise ValueError("itinerary_auto_care requires a verified itinerary_journey_id")
        return self


class Interruption(FrozenModel):
    kind: Literal["decision_required", "rule_required", "stop_condition", "boundary"]
    tick: int = Field(ge=-(2**63), le=2**63 - 1)
    reason_code: str = Field(min_length=1, max_length=120)
    subject_id: UUID | None = None
    reference_id: UUID | None = None


class WorkPurchaseAffordabilityReceipt(FrozenModel):
    """Historical evidence that a named market target was affordable."""

    listing_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    currency_resource_id: ResourceName
    total_price_minor: ExactDecimal = Field(ge=Decimal("0"))
    wage_balance: ExactDecimal = Field(ge=Decimal("0"))
    confirmed_tick: StrictInt = Field(ge=-(2**63), le=2**63 - 1)

    @model_validator(mode="after")
    def validates_affordability(self) -> WorkPurchaseAffordabilityReceipt:
        if self.total_price_minor != self.total_price_minor.to_integral_value():
            raise ValueError("the purchase affordability receipt total must be an integer minor-unit amount")
        if self.wage_balance < self.total_price_minor:
            raise ValueError("the recorded wage balance must cover the purchase target quote")
        return self


class ResolutionMetrics(FrozenModel):
    from_tick: int
    to_tick: int
    ticks_advanced: int = Field(ge=0)
    integration_boundary: str
    event_count: int = Field(ge=0)
    depth: Literal["standard", "deep", "exhaustive"]
    speed: Literal["fast", "balanced", "patient"]
    needs_changed: tuple[str, ...] = ()
    resources_consumed: tuple[tuple[str, str], ...] = ()
    work_purchase_affordability: WorkPurchaseAffordabilityReceipt | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class SimulationResolution(FrozenModel):
    outcome: Literal["advanced", "completed", "stopped", "decision_required", "rule_required"]
    events: tuple[DomainEvent, ...]
    next_tick: int
    interruption: Interruption | None = None
    decision_requests: tuple[DecisionRequest, ...] = ()
    accepted_decisions: tuple[DecisionResponse, ...] = ()
    continuation: SimulationPlan | None = None
    rule_resolution: RuleResolution | None = None
    metrics: ResolutionMetrics
    _itinerary_care_receipt: object | None = PrivateAttr(default=None)
    _itinerary_transit_projection_receipt: object | None = PrivateAttr(default=None)
