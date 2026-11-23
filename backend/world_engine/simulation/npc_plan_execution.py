"""Pure, revalidated event proposals for persistent NPC plan steps.

This layer turns one due plan revision into a batch of already registered
domain events. It never commits those events and never advances the world
clock. A host must compare the returned source hash, revision, and context to
its leased state before atomically storing the proposed batch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import Field

from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.gameplay_events import MarketTradeExecutedEvent, ResourceProducedEvent
from world_engine.domain.investigation import entity_visible_to_actor
from world_engine.domain.models import DomainEvent, WorldState
from world_engine.domain.npc_goal_completion import (
    NPC_GOAL_DEPENDENCY_COMPLETION_POLICY, npc_goal_conditions_met, npc_goal_fulfillment_allows_continuation,
    npc_goal_fulfillment_closure, npc_goal_fulfillment_enabled,
)
from world_engine.domain.npc_plan_events import NpcPlanRevisedEvent, npc_plan_event_id
from world_engine.domain.npc_plan_review import build_npc_goal_followup_review, with_npc_failure_review
from world_engine.domain.npc_plans import (
    ActiveNpcExecution, ActiveNpcExploration, ActiveNpcProduction, ActiveNpcTrade, ActiveNpcTraining, NPC_EXPLORATION_TICKS,
    NPC_SHARED_EXPLORATION_POLICY, NPC_SHARED_PRODUCTION_POLICY, NPC_SHARED_TRADE_POLICY, NPC_SHARED_TRAINING_POLICY, NPC_TRADE_TICKS,
    NpcPlan, NpcTimedExecution,
)
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent, travel_event_id
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)
from world_engine.rules.contracts import ExploreIntent, ProduceIntent, RuleContext, RuleResolution, TradeIntent, TrainIntent
from world_engine.rules.engine import RuleEngine

from .contracts import FrozenModel, SimulationContext
from .npc_capabilities import (
    NpcCapabilityRegistry,
    build_default_npc_capability_registry,
    preflight_npc_plan_step,
)


NpcPlanExecutionOutcome = Literal[
    "not_due",
    "step_completed",
    "journey_started",
    "waiting_for_journey",
    "journey_completed",
    "journey_interrupted",
    "exploration_started",
    "exploration_resolved",
    "trade_started",
    "trade_resolved",
    "training_started",
    "training_resolved",
    "production_started",
    "production_resolved",
    "blocked",
    "cancelled",
    "abandoned",
    "interval_required",
    "terminal",
    "invalid_plan",
    "invalid_receipt",
    "invalid_proposal",
]

_INITIAL_RETRY_TICKS = 60
_MAX_RETRY_TICKS = 86_400
_MAX_PLAN_EVIDENCE = 64
_MAX_PLAN_REVISION = 2**63 - 1
_MAX_ATTEMPTS = 2**63 - 1
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)


class NpcPlanTransition(FrozenModel):
    """Validated proposal metadata and original rule evidence for one plan."""

    outcome: NpcPlanExecutionOutcome
    events: tuple[DomainEvent, ...] = ()
    next_due_tick: int | None = Field(default=None, ge=_MIN_TICK, le=_MAX_TICK)
    required_ticks: int | None = Field(default=None, ge=0, le=_MAX_TICK)
    reason_code: str | None = Field(default=None, min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=2000)
    plan_id: UUID
    actor_id: UUID | None = None
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    state_version: int = Field(ge=0)
    tick_before: int = Field(ge=_MIN_TICK, le=_MAX_TICK)
    tick_after: int = Field(ge=_MIN_TICK, le=_MAX_TICK)
    source_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_state_hash_after: str = Field(pattern=r"^[a-f0-9]{64}$")
    projected_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: int | None = Field(default=None, ge=1, le=_MAX_PLAN_REVISION)
    result_revision: int | None = Field(default=None, ge=1, le=_MAX_PLAN_REVISION)
    source_current_step: int | None = Field(default=None, ge=0)
    result_current_step: int | None = Field(default=None, ge=0)
    consumed_step_index: int | None = Field(default=None, ge=0)
    evidence_event_ids: tuple[UUID, ...] = ()
    journey_id: UUID | None = None
    capability_id: str | None = None
    capability_registry_version: str | None = None
    action_kind: str | None = None
    rule_resolution: RuleResolution | None = None
    committed: Literal[False] = False
    time_advanced: Literal[False] = False


def build_npc_plan_execution_transition(
    state: WorldState,
    plan_id: UUID,
    context: SimulationContext,
    *,
    registry: NpcCapabilityRegistry | None = None,
    rule_engine: RuleEngine | None = None,
    journey_resolution_receipts: Mapping[UUID, TravelJourneyChangedEvent] | None = None,
) -> NpcPlanTransition:
    """Build one pure transition proposal for a due NPC plan.

    Each emitted event is applied in order to a temporary state before it is
    returned. The input state is never mutated. Arrival is consumed only when
    the caller supplies the actual resolution receipt and the current state
    independently projects that exact resolved journey.

    The state projection does not carry branch ancestry. Before calling this
    function, the host must verify that the active execution and journey are
    visible on the current branch, then scope ``journey_resolution_receipts``
    to the current branch and its committed ancestor prefix. This layer
    accepts only the departure branch, current branch, or a host-attested
    receipt branch from ``context.journey_receipt_branch_ids``.
    """

    before_hash = state_hash(state)
    plans = [plan for plan in state.npc_plans if plan.plan_id == plan_id]
    if len(plans) != 1:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan_id=plan_id,
            actor_id=None,
            reason_code="npc_plan_missing_or_ambiguous",
        )
    plan = plans[0]

    if plan.updated_tick > state.current_tick or plan.created_tick > state.current_tick:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan=plan,
            reason_code="npc_plan_time_is_in_future",
        )
    if plan.status in {"completed", "cancelled", "abandoned"}:
        return _empty_transition(
            state, context, before_hash, outcome="terminal", plan=plan,
        )
    if plan.revision >= _MAX_PLAN_REVISION:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan=plan,
            reason_code="npc_plan_revision_exhausted",
        )

    actor = state.entities.get(plan.actor_id)
    if actor is None or actor.kind not in {"character", "person"}:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan=plan,
            reason_code="npc_plan_actor_missing",
        )
    goals_by_id = {goal.id: goal for goal in state.goals}
    if any(
        goal_id not in goals_by_id or goals_by_id[goal_id].owner_id != plan.actor_id
        for goal_id in plan.goal_ids
    ):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan=plan,
            reason_code="npc_plan_goal_missing_or_mismatched",
        )

    terminal = _termination_cause(state, plan, context)
    if terminal is not None:
        status, reason_code, reason = terminal
        return _revise_terminal(
            state, plan, context, before_hash,
            status=status,
            reason_code=reason_code,
            reason=reason,
            control_interruption=(
                "account_control" if reason_code == "npc_plan_actor_externally_controlled" else None
            ),
        )

    if plan.next_review_tick is None:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan=plan,
            reason_code="npc_plan_review_tick_missing",
        )
    if state.current_tick < plan.next_review_tick:
        return _empty_transition(
            state, context, before_hash, outcome="not_due", plan=plan,
            next_due_tick=plan.next_review_tick,
        )

    if plan.status == "running":
        if plan.active_execution is None:
            return _empty_transition(
                state, context, before_hash, outcome="invalid_plan", plan=plan,
                reason_code="running_npc_plan_execution_missing",
            )
        if isinstance(plan.active_execution, ActiveNpcExploration):
            return _finish_exploration(state, plan, context, before_hash, rule_engine or RuleEngine(), plan.active_execution)
        if isinstance(plan.active_execution, ActiveNpcTrade):
            return _finish_trade(state, plan, context, before_hash, rule_engine or RuleEngine(), plan.active_execution)
        if isinstance(plan.active_execution, ActiveNpcTraining):
            return _finish_training(state, plan, context, before_hash, rule_engine or RuleEngine(), plan.active_execution)
        if isinstance(plan.active_execution, ActiveNpcProduction):
            return _finish_production(state, plan, context, before_hash, rule_engine or RuleEngine(), plan.active_execution)
        receipts = (
            journey_resolution_receipts
            if journey_resolution_receipts is not None
            else context.journey_resolution_receipts
        )
        return _resolve_active_journey(
            state,
            plan,
            context,
            before_hash,
            receipts,
        )

    # Only due ready/blocked plans reach preflight. Capability resolution is
    # repeated against the complete current state on every attempt.
    active_registry = registry or build_default_npc_capability_registry()
    engine = rule_engine or RuleEngine()
    step = plan.steps[plan.current_step]
    rule_context = RuleContext(
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        actor_id=plan.actor_id,
        ruleset_version=context.ruleset_version,
    )
    try:
        preflight = preflight_npc_plan_step(
            state,
            plan.actor_id,
            step,
            rule_context,
            rule_engine=engine,
            registry=active_registry,
            externally_controlled_actor_ids=context.externally_controlled_actor_ids,
        )
    except Exception:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_capability_preflight_failed",
        )

    if (
        preflight.state_hash_before != before_hash
        or preflight.state_hash_after != before_hash
        or preflight.resolution.ruleset_version != context.ruleset_version
    ):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_capability_preflight_scope_mismatch",
            preflight=preflight,
        )

    if preflight.status == "interval_required":
        if preflight.action_kind == "explore" and state.metadata.get(NPC_SHARED_EXPLORATION_POLICY) is True:
            return _start_exploration(state, plan, context, before_hash)
        if preflight.action_kind == "trade" and state.metadata.get(NPC_SHARED_TRADE_POLICY) is True:
            return _start_trade(state, plan, context, before_hash, preflight.resolution)
        if preflight.action_kind == "train" and state.metadata.get(NPC_SHARED_TRAINING_POLICY) is True:
            return _start_training(state, plan, context, before_hash)
        if preflight.action_kind == "produce" and state.metadata.get(NPC_SHARED_PRODUCTION_POLICY) is True:
            return _start_production(state, plan, context, before_hash, preflight)
        return _empty_transition(
            state, context, before_hash, outcome="interval_required", plan=plan,
            required_ticks=preflight.required_ticks,
            reason_code="npc_action_requires_interval_adapter",
            reason=preflight.resolution.outcome.message,
            preflight=preflight,
        )

    if preflight.status == "rejected":
        code = preflight.resolution.outcome.code
        message = preflight.resolution.outcome.message
        return _revise_blocked(
            state, plan, context, before_hash,
            reason_code=code,
            reason=message,
            preflight=preflight,
        )

    if preflight.status == "journey_start":
        return _start_journey(
            state, plan, context, before_hash, preflight,
        )

    if preflight.status == "instant":
        return _execute_instant(
            state, plan, context, before_hash, preflight,
        )

    return _empty_transition(
        state, context, before_hash, outcome="invalid_proposal", plan=plan,
        reason_code="npc_capability_status_unrecognized",
        preflight=preflight,
    )


def _termination_cause(
    state: WorldState,
    plan: NpcPlan,
    context: SimulationContext,
) -> tuple[Literal["cancelled", "abandoned"], str, str] | None:
    if plan.actor_id == state.player_id:
        return "abandoned", "npc_plan_actor_is_player", "The player character cannot be controlled by NPC plan execution."
    if plan.actor_id in context.externally_controlled_actor_ids:
        return "abandoned", "npc_plan_actor_externally_controlled", "The actor is under direct account control."
    actor = state.entities.get(plan.actor_id)
    if actor is not None and actor.status != "active":
        return "cancelled", "npc_plan_actor_unavailable", f"The actor is {actor.status}; the plan was cancelled."

    goals = {goal.id: goal for goal in state.goals}
    for goal_id in plan.goal_ids:
        goal = goals.get(goal_id)
        if goal is None or goal.owner_id != plan.actor_id:
            return None
        if npc_goal_fulfillment_allows_continuation(state, goal):
            continue
        if goal.status != "active" or (goal.deadline is not None and goal.deadline < state.current_tick):
            return "cancelled", "npc_plan_goal_unavailable", "A linked goal is no longer active and pursuable."
    return None


def _revise_terminal(
    state: WorldState,
    plan: NpcPlan,
    context: SimulationContext,
    before_hash: str,
    *,
    status: Literal["cancelled", "abandoned"],
    reason_code: str,
    reason: str,
    control_interruption: Literal["account_control"] | None = None,
) -> NpcPlanTransition:
    revised = _rebuild_plan(
        plan,
        revision=plan.revision + 1,
        status=status,
        updated_tick=state.current_tick,
        next_review_tick=None,
        last_failure=reason,
        active_execution=None,
    )
    return _validated_transition(
        state,
        context,
        before_hash,
        plan,
        [
            _revision_event(
                revised, plan.revision, context,
                control_interruption=control_interruption,
            ),
        ],
        outcome=status,
        reason_code=reason_code,
        reason=reason,
    )


def _revise_blocked(
    state: WorldState,
    plan: NpcPlan,
    context: SimulationContext,
    before_hash: str,
    *,
    reason_code: str,
    reason: str,
    preflight,
) -> NpcPlanTransition:
    next_tick = _retry_tick(state.current_tick, plan.attempts)
    if next_tick is None:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_plan_retry_tick_overflow", preflight=preflight,
        )
    failure = f"{reason_code}: {reason}"[:2000]
    revised = _rebuild_plan(
        plan,
        revision=plan.revision + 1,
        status="blocked",
        updated_tick=state.current_tick,
        next_review_tick=next_tick,
        attempts=min(_MAX_ATTEMPTS, plan.attempts + 1),
        last_failure=failure,
        active_execution=None,
    )
    revision_event = with_npc_failure_review(state, _revision_event(revised, plan.revision, context))
    transition = _validated_transition(
        state,
        context,
        before_hash,
        plan,
        [revision_event],
        outcome="blocked",
        next_due_tick=revision_event.plan.next_review_tick,
        reason_code=reason_code,
        reason=reason,
        preflight=preflight,
    )
    return transition


def _start_exploration(state, plan, context, before_hash) -> NpcPlanTransition:
    intent = ExploreIntent.model_validate(plan.steps[plan.current_step].arguments)
    started = ActiveNpcExploration(
        place_id=intent.place_id, **_timed_start_fields(state, plan, context, NPC_EXPLORATION_TICKS),
    )
    return _start_timed_action(state, plan, context, before_hash, started)


_TRADE_QUOTE_FIELDS = (
    "market_id", "listing_id", "seller_id", "stock_account_id", "resource_id", "currency_resource_id", "unit_price_minor",
)


def _start_trade(state, plan, context, before_hash, quote) -> NpcPlanTransition:
    trades = [event for event in quote.events if isinstance(event, MarketTradeExecutedEvent)]
    if len(trades) != 1:
        return _empty_transition(state, context, before_hash, outcome="invalid_proposal", plan=plan,
                                 reason_code="npc_trade_quote_missing")
    started = ActiveNpcTrade(
        **{key: getattr(trades[0], key) for key in _TRADE_QUOTE_FIELDS},
        **_timed_start_fields(state, plan, context, NPC_TRADE_TICKS),
    )
    return _start_timed_action(state, plan, context, before_hash, started)


def _start_training(state, plan, context, before_hash) -> NpcPlanTransition:
    intent = TrainIntent.model_validate(plan.steps[plan.current_step].arguments)
    started = ActiveNpcTraining(
        skill_id=intent.skill_id,
        place_id=state.entities[plan.actor_id].location_id,
        **_timed_start_fields(state, plan, context, intent.duration_ticks),
    )
    return _start_timed_action(state, plan, context, before_hash, started)


def _start_production(state, plan, context, before_hash, preflight: NpcCapabilityPreflight) -> NpcPlanTransition:
    intent = ProduceIntent.model_validate(plan.steps[plan.current_step].arguments)
    started = ActiveNpcProduction(
        producer_id=intent.producer_id, recipe_id=intent.recipe_id,
        recipe_terms_hash=preflight.resolution.outcome.details["recipe_terms_hash"],
        place_id=state.entities[plan.actor_id].location_id,
        **_timed_start_fields(state, plan, context, preflight.required_ticks),
    )
    return _start_timed_action(state, plan, context, before_hash, started)


def _timed_start_fields(state, plan, context, duration):
    revision = plan.revision + 1
    return {
        "start_event_id": npc_plan_event_id(context.command_id, context.ruleset_version, "revised", plan.plan_id, revision),
        "start_command_id": context.command_id,
        "world_id": context.world_id,
        "branch_id": context.branch_id,
        "ruleset_version": context.ruleset_version,
        "started_tick": state.current_tick,
        "due_tick": state.current_tick + duration,
    }


def _start_timed_action(state, plan, context, before_hash, started: NpcTimedExecution) -> NpcPlanTransition:
    # Search can emit two discovery events, trade up to three account openings and an exchange.
    max_evidence = 5 if isinstance(started, ActiveNpcTrade) else 3
    if plan.revision >= _MAX_PLAN_REVISION - 1 or len(plan.evidence_refs) > _MAX_PLAN_EVIDENCE - max_evidence:
        return _revise_terminal(state, plan, context, before_hash, status="abandoned",
                                reason_code="npc_plan_evidence_limit", reason="The plan has insufficient space for another receipt.")
    running = _rebuild_plan(
        plan, revision=plan.revision + 1, status="running", updated_tick=state.current_tick,
        next_review_tick=started.due_tick, attempts=min(_MAX_ATTEMPTS, plan.attempts + 1),
        last_failure=None, active_execution=started,
    )
    return _validated_transition(
        state, context, before_hash, plan, [_revision_event(running, plan.revision, context)],
        outcome=f"{started.kind}_started",
        next_due_tick=started.due_tick, required_ticks=started.due_tick - started.started_tick,
    )


def _finish_exploration(state, plan, context, before_hash, engine, active) -> NpcPlanTransition:
    actor = state.entities[plan.actor_id]
    intent = ExploreIntent.model_validate(plan.steps[plan.current_step].arguments)
    if (
        active.world_id != context.world_id or active.ruleset_version != context.ruleset_version
        or active.place_id != intent.place_id or active.due_tick != state.current_tick
    ):
        return _empty_transition(state, context, before_hash, outcome="invalid_receipt", plan=plan,
                                 reason_code="npc_exploration_scope_mismatch")
    start_context = RuleContext(
        world_id=active.world_id, branch_id=active.branch_id, command_id=active.start_command_id,
        actor_id=actor.id, ruleset_version=active.ruleset_version,
    )
    resolution = engine.settle_exploration(state, intent, start_context)
    if resolution.outcome.status == "rejected":
        return _settle_timed_action(state, plan, context, before_hash, "exploration", "interrupted", None)
    return _settle_timed_action(state, plan, context, before_hash, "exploration", resolution.outcome.code, resolution)


def _finish_trade(state, plan, context, before_hash, engine, active: ActiveNpcTrade) -> NpcPlanTransition:
    actor = state.entities[plan.actor_id]
    intent = TradeIntent.model_validate(plan.steps[plan.current_step].arguments)
    if (
        active.world_id != context.world_id or active.ruleset_version != context.ruleset_version
        or active.listing_id != intent.listing_id or active.due_tick != state.current_tick
    ):
        return _empty_transition(state, context, before_hash, outcome="invalid_receipt", plan=plan,
                                 reason_code="npc_trade_scope_mismatch")
    if actor.location_id != active.market_id:
        return _settle_timed_action(state, plan, context, before_hash, "trade", "market_location_changed", None)
    rule_context = RuleContext(
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
        actor_id=actor.id, ruleset_version=context.ruleset_version,
    )
    settlement = engine._trade_settlement(state, actor, intent, rule_context)
    if settlement.outcome.status == "rejected":
        return _settle_timed_action(state, plan, context, before_hash, "trade", settlement.outcome.code, None)
    trades = [event for event in settlement.events if isinstance(event, MarketTradeExecutedEvent)]
    if len(trades) != 1 or any(getattr(trades[0], key) != getattr(active, key) for key in _TRADE_QUOTE_FIELDS):
        return _settle_timed_action(state, plan, context, before_hash, "trade", "market_quote_changed", None)
    return _settle_timed_action(state, plan, context, before_hash, "trade", "trade_completed", settlement)


def _finish_training(state, plan, context, before_hash, engine, active: ActiveNpcTraining) -> NpcPlanTransition:
    actor = state.entities[plan.actor_id]
    intent = TrainIntent.model_validate(plan.steps[plan.current_step].arguments)
    if (
        active.world_id != context.world_id or active.ruleset_version != context.ruleset_version
        or active.skill_id != intent.skill_id or active.due_tick != state.current_tick
        or active.due_tick - active.started_tick != intent.duration_ticks
    ):
        return _empty_transition(state, context, before_hash, outcome="invalid_receipt", plan=plan,
                                 reason_code="npc_training_scope_mismatch")
    place = state.entities.get(active.place_id)
    if actor.location_id != active.place_id or (active.place_id is not None and (place is None or place.status != "active")):
        return _settle_timed_action(state, plan, context, before_hash, "training", "interrupted", None)
    rule_context = RuleContext(
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
        actor_id=actor.id, ruleset_version=context.ruleset_version,
    )
    progress = engine._training_progress(actor, intent, rule_context, trained_at_tick=state.current_tick)
    return _settle_timed_action(state, plan, context, before_hash, "training", progress.outcome.code, progress)


def _finish_production(state, plan, context, before_hash, engine, active: ActiveNpcProduction) -> NpcPlanTransition:
    actor = state.entities[plan.actor_id]
    intent = ProduceIntent.model_validate(plan.steps[plan.current_step].arguments)
    if (
        active.world_id != context.world_id or active.ruleset_version != context.ruleset_version
        or active.due_tick != state.current_tick
        or (active.producer_id, active.recipe_id) != (intent.producer_id, intent.recipe_id)
    ):
        return _empty_transition(state, context, before_hash, outcome="invalid_receipt", plan=plan,
                                 reason_code="npc_production_scope_mismatch")
    place = state.entities.get(active.place_id)
    if (
        actor.location_id != active.place_id
        or active.place_id is not None and (place is None or place.status != "active")
        or intent.producer_id != actor.id and not entity_visible_to_actor(state, actor.id, intent.producer_id)
    ):
        return _settle_timed_action(state, plan, context, before_hash, "production", "interrupted", None)
    rule_context = RuleContext(
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
        actor_id=actor.id, ruleset_version=context.ruleset_version,
    )
    settlement = engine._production_settlement(state, actor, intent, rule_context)
    if settlement.outcome.status == "rejected":
        return _settle_timed_action(state, plan, context, before_hash, "production", settlement.outcome.code, None)
    terms = settlement.outcome.details
    if (
        terms["recipe_terms_hash"] != active.recipe_terms_hash
        or terms["duration_seconds"] != active.due_tick - active.started_tick
    ):
        return _settle_timed_action(state, plan, context, before_hash, "production", "recipe_changed", None)
    return _settle_timed_action(state, plan, context, before_hash, "production", "production_completed", settlement)


def _settle_timed_action(state, plan, context, before_hash, kind, code, resolution) -> NpcPlanTransition:
    consumed = code in {"discovered", "nothing_hidden_found", "trade_completed", "training_completed", "production_completed"}
    status = ("completed" if plan.current_step + 1 == len(plan.steps) else "ready") if consumed else "blocked"
    next_tick = (None if status == "completed" else state.current_tick) if consumed else _retry_tick(state.current_tick, plan.attempts)
    if next_tick is None and status == "blocked":
        return _revise_terminal(state, plan, context, before_hash, status="cancelled",
                                reason_code="npc_plan_retry_tick_overflow", reason="The action cannot be retried beyond the world clock range.")
    core_events = resolution.events if resolution is not None else []
    receipt_id = npc_plan_event_id(context.command_id, context.ruleset_version, "revised", plan.plan_id, plan.revision + 1)
    evidence_events = [event for event in core_events if isinstance(event, ResourceProducedEvent)] if kind == "production" else core_events
    evidence = (*_actual_event_ids(evidence_events), receipt_id) if consumed else ()
    revised = _rebuild_plan(
        plan, revision=plan.revision + 1, status=status, updated_tick=state.current_tick,
        next_review_tick=next_tick, current_step=plan.current_step + int(consumed),
        active_execution=None, last_failure=None if consumed else f"npc_{kind}_{code}",
        evidence_refs=[*plan.evidence_refs, *evidence],
    )
    revision_event = with_npc_failure_review(state, _revision_event(
        revised, plan.revision, context,
        exploration_result=code if kind == "exploration" else None,
        trade_result=code if kind == "trade" else None,
        training_result=code if kind == "training" else None,
        production_result=code if kind == "production" else None,
    ))
    return _validated_transition(
        state, context, before_hash, plan,
        [*core_events, revision_event],
        outcome=f"{kind}_resolved",
        next_due_tick=revision_event.plan.next_review_tick, reason_code=code,
        consumed_step_index=plan.current_step if consumed else None,
        evidence_event_ids=evidence, rule_resolution=resolution,
    )


def _execute_instant(state, plan, context, before_hash, preflight) -> NpcPlanTransition:
    resolution = preflight.resolution
    if plan.revision >= _MAX_PLAN_REVISION - 1:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_plan_revision_exhausted",
            preflight=preflight,
        )
    event_ids = _actual_event_ids(resolution.events)
    if resolution.outcome.status != "succeeded" or resolution.elapsed_ticks != 0 or not event_ids:
        code = "successful_npc_action_without_evidence" if not event_ids else "npc_action_not_successful"
        reason = "A zero-time NPC action must succeed with at least one actual rule event."
        return _revise_blocked(
            state, plan, context, before_hash,
            reason_code=code,
            reason=reason,
            preflight=preflight,
        )
    if any(event_id in plan.evidence_refs for event_id in event_ids):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_rule_event_reused_as_evidence",
            preflight=preflight,
        )

    next_evidence = _append_evidence(plan.evidence_refs, event_ids)
    if next_evidence is None:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_plan_evidence_limit",
            preflight=preflight,
        )

    attempt_count = min(_MAX_ATTEMPTS, plan.attempts + 1)
    running = _rebuild_plan(
        plan,
        revision=plan.revision + 1,
        status="running",
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick,
        attempts=attempt_count,
        last_failure=None,
        active_execution=None,
    )
    final_status: Literal["ready", "completed"] = (
        "completed" if plan.current_step + 1 == len(plan.steps) else "ready"
    )
    consumed = _rebuild_plan(
        running,
        revision=running.revision + 1,
        status=final_status,
        updated_tick=state.current_tick,
        next_review_tick=None if final_status == "completed" else state.current_tick,
        current_step=plan.current_step + 1,
        last_failure=None,
        evidence_refs=next_evidence,
        active_execution=None,
    )
    events: list[DomainEvent] = [
        _revision_event(running, plan.revision, context),
        *resolution.events,
        _revision_event(consumed, running.revision, context),
    ]
    return _validated_transition(
        state,
        context,
        before_hash,
        plan,
        events,
        outcome="step_completed",
        next_due_tick=consumed.next_review_tick,
        reason_code="npc_instant_step_completed",
        consumed_step_index=plan.current_step,
        evidence_event_ids=tuple(event_ids),
        preflight=preflight,
    )


def _start_journey(state, plan, context, before_hash, preflight) -> NpcPlanTransition:
    resolution = preflight.resolution
    departures = [event for event in resolution.events if isinstance(event, TravelJourneyChangedEvent)]
    if (
        resolution.outcome.status != "succeeded"
        or resolution.elapsed_ticks != 0
        or len(departures) != 1
        or departures[0].operation != "depart"
        or len(resolution.events) != 1
    ):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_journey_departure_event_invalid",
            preflight=preflight,
        )
    departure = departures[0]
    journey = departure.journey
    due_tick = min(
        tick for tick in (journey.hazard_tick, journey.arrival_tick) if tick is not None
    )
    if (
        departure.world_id != context.world_id
        or departure.branch_id != context.branch_id
        or departure.command_id != context.command_id
        or departure.ruleset_version != context.ruleset_version
        or departure.actor_id != plan.actor_id
        or journey.departed_tick != state.current_tick
        or due_tick <= state.current_tick
    ):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_journey_departure_scope_mismatch",
            preflight=preflight,
        )
    evidence = _append_evidence(plan.evidence_refs, [departure.event_id])
    if evidence is None or departure.event_id in plan.evidence_refs:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_plan_evidence_limit_or_duplicate",
            preflight=preflight,
        )
    active = ActiveNpcExecution(
        kind="journey",
        journey_id=journey.journey_id,
        start_event_id=departure.event_id,
        start_command_id=departure.command_id,
        world_id=departure.world_id,
        branch_id=departure.branch_id,
        ruleset_version=departure.ruleset_version,
        started_tick=journey.departed_tick,
        due_tick=due_tick,
    )
    running = _rebuild_plan(
        plan,
        revision=plan.revision + 1,
        status="running",
        updated_tick=state.current_tick,
        next_review_tick=due_tick,
        attempts=min(_MAX_ATTEMPTS, plan.attempts + 1),
        last_failure=None,
        evidence_refs=evidence,
        active_execution=active,
    )
    # Applying the real departure first means the running record never points
    # at a journey that is absent from the scratch projection.
    events: list[DomainEvent] = [departure, _revision_event(running, plan.revision, context)]
    return _validated_transition(
        state,
        context,
        before_hash,
        plan,
        events,
        outcome="journey_started",
        next_due_tick=due_tick,
        required_ticks=preflight.required_ticks,
        reason_code="npc_journey_started",
        evidence_event_ids=(departure.event_id,),
        journey_id=journey.journey_id,
        preflight=preflight,
    )


def _resolve_active_journey(
    state: WorldState,
    plan: NpcPlan,
    context: SimulationContext,
    before_hash: str,
    receipts: Mapping[UUID, TravelJourneyChangedEvent],
) -> NpcPlanTransition:
    active = plan.active_execution
    assert active is not None
    journeys = [journey for journey in state.travel_journeys if journey.journey_id == active.journey_id]
    if len(journeys) != 1:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan=plan,
            reason_code="npc_active_journey_missing_or_ambiguous",
            journey_id=active.journey_id,
        )
    journey = journeys[0]
    if not _active_execution_matches_state(plan, active, journey, context):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_plan", plan=plan,
            reason_code="npc_active_journey_scope_or_record_mismatch",
            journey_id=active.journey_id,
        )

    if journey.status == "in_transit":
        return _empty_transition(
            state, context, before_hash, outcome="waiting_for_journey", plan=plan,
            next_due_tick=active.due_tick,
            reason_code=(
                "npc_journey_resolution_overdue" if state.current_tick > active.due_tick
                else "npc_journey_not_arrived"
            ),
            journey_id=active.journey_id,
        )

    raw_receipt = receipts.get(active.journey_id)
    if raw_receipt is None:
        return _empty_transition(
            state, context, before_hash, outcome="waiting_for_journey", plan=plan,
            next_due_tick=state.current_tick,
            reason_code="npc_journey_resolution_receipt_missing",
            journey_id=active.journey_id,
        )
    try:
        receipt = TravelJourneyChangedEvent.model_validate(
            raw_receipt.model_dump(mode="python")
            if isinstance(raw_receipt, TravelJourneyChangedEvent)
            else raw_receipt
        )
    except Exception:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_receipt", plan=plan,
            reason_code="npc_journey_resolution_receipt_invalid",
            journey_id=active.journey_id,
        )
    if not _receipt_matches_state(receipt, journey, plan, active, context, state):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_receipt", plan=plan,
            reason_code="npc_journey_resolution_receipt_mismatch",
            journey_id=active.journey_id,
        )

    next_evidence = _append_evidence(plan.evidence_refs, [receipt.event_id])
    if next_evidence is None:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=plan,
            reason_code="npc_plan_evidence_limit",
            journey_id=active.journey_id,
        )

    if receipt.operation == "interrupt":
        next_tick = _retry_tick(state.current_tick, plan.attempts)
        if next_tick is None:
            return _empty_transition(
                state, context, before_hash, outcome="invalid_proposal", plan=plan,
                reason_code="npc_plan_retry_tick_overflow",
                journey_id=active.journey_id,
            )
        failure = f"travel_interrupted: Journey {active.journey_id} was interrupted at tick {journey.resolved_tick}."
        revised = _rebuild_plan(
            plan,
            revision=plan.revision + 1,
            status="blocked",
            updated_tick=state.current_tick,
            next_review_tick=next_tick,
            last_failure=failure,
            evidence_refs=next_evidence,
            active_execution=None,
        )
        revision_event = with_npc_failure_review(state, _revision_event(revised, plan.revision, context))
        return _validated_transition(
            state,
            context,
            before_hash,
            plan,
            [revision_event],
            outcome="journey_interrupted",
            next_due_tick=revision_event.plan.next_review_tick,
            reason_code="npc_journey_interrupted",
            reason=failure,
            evidence_event_ids=(receipt.event_id,),
            journey_id=active.journey_id,
        )

    completed_status: Literal["ready", "completed"] = (
        "completed" if plan.current_step + 1 == len(plan.steps) else "ready"
    )
    revised = _rebuild_plan(
        plan,
        revision=plan.revision + 1,
        status=completed_status,
        updated_tick=state.current_tick,
        next_review_tick=None if completed_status == "completed" else state.current_tick,
        current_step=plan.current_step + 1,
        last_failure=None,
        evidence_refs=next_evidence,
        active_execution=None,
    )
    return _validated_transition(
        state,
        context,
        before_hash,
        plan,
        [_revision_event(revised, plan.revision, context)],
        outcome="journey_completed",
        next_due_tick=revised.next_review_tick,
        reason_code="npc_journey_arrived",
        consumed_step_index=plan.current_step,
        evidence_event_ids=(receipt.event_id,),
        journey_id=active.journey_id,
        arrived_at=journey.to_place_id,
    )


def _active_execution_matches_state(
    plan: NpcPlan,
    active: ActiveNpcExecution,
    journey: TravelJourney,
    context: SimulationContext,
) -> bool:
    try:
        expected_start_id = travel_event_id(
            active.start_command_id,
            active.ruleset_version,
            "depart",
            active.journey_id,
        )
    except ValueError:
        return False
    return bool(
        active.start_event_id == expected_start_id
        and active.world_id == context.world_id
        and active.ruleset_version == context.ruleset_version
        and active.journey_id == journey.journey_id
        and journey.actor_id == plan.actor_id
        and journey.departed_tick == active.started_tick
        and active.due_tick == min(
            tick for tick in (journey.hazard_tick, journey.arrival_tick) if tick is not None
        )
        and plan.next_review_tick == active.due_tick
    )


def _receipt_matches_state(
    receipt: TravelJourneyChangedEvent,
    journey: TravelJourney,
    plan: NpcPlan,
    active: ActiveNpcExecution,
    context: SimulationContext,
    state: WorldState,
) -> bool:
    if (
        receipt.operation not in {"arrive", "interrupt"}
        or receipt.world_id != active.world_id
        or receipt.world_id != context.world_id
        or receipt.branch_id not in {
            active.branch_id,
            context.branch_id,
            *context.journey_receipt_branch_ids,
        }
        or receipt.ruleset_version != active.ruleset_version
        or receipt.actor_id != plan.actor_id
        or receipt.journey.journey_id != active.journey_id
        or receipt.journey != journey
        or journey.status not in {"arrived", "interrupted"}
        or state.current_tick < (journey.resolved_tick if journey.resolved_tick is not None else _MAX_TICK)
    ):
        return False
    expected_operation = "arrive" if journey.status == "arrived" else "interrupt"
    if receipt.operation != expected_operation:
        return False
    schedule_id = travel_resolution_schedule_id(journey.journey_id)
    schedules = [row for row in state.scheduled_events if row.id == schedule_id]
    if len(schedules) != 1:
        return False
    schedule = schedules[0]
    if not _resolved_schedule_matches(schedule, journey):
        return False
    actor = state.entities.get(plan.actor_id)
    if actor is None:
        return False
    expected_location = (
        journey.to_place_id if journey.status == "arrived"
        else journey.from_place_id if actor.status not in {"dead", "retired"}
        else None
    )
    return actor.location_id == expected_location


def _resolved_schedule_matches(schedule: ScheduledEventRecord, journey: TravelJourney) -> bool:
    due_tick = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
    return bool(
        schedule.status == "completed"
        and schedule.handler_id == TRAVEL_RESOLUTION_HANDLER_ID
        and schedule.handler_version == TRAVEL_RESOLUTION_HANDLER_VERSION
        and schedule.phase == "resolution"
        and schedule.due_tick == due_tick
        and schedule.max_runs == 1
        and schedule.repeat_ticks is None
        and schedule.completed_runs == 1
        and schedule.payload == {"journey_id": str(journey.journey_id)}
    )


def _actual_event_ids(events: list[DomainEvent]) -> list[UUID]:
    ids: list[UUID] = []
    for event in events:
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, UUID):
            return []
        ids.append(event_id)
    if len(ids) != len(set(ids)):
        return []
    return ids


def _append_evidence(existing: list[UUID], additions: list[UUID]) -> list[UUID] | None:
    if len(set(additions)) != len(additions):
        return None
    if any(event_id in existing for event_id in additions):
        return None
    combined = [*existing, *additions]
    if len(combined) > _MAX_PLAN_EVIDENCE:
        return None
    return combined


def _retry_tick(current_tick: int, attempts: int) -> int | None:
    exponent = min(max(attempts, 0), 10)
    delay = min(_MAX_RETRY_TICKS, _INITIAL_RETRY_TICKS * (2**exponent))
    due = current_tick + max(1, delay)
    return due if current_tick < due <= _MAX_TICK else None


def _rebuild_plan(plan: NpcPlan, **updates) -> NpcPlan:
    payload = plan.model_dump(mode="python")
    payload.update(updates)
    return NpcPlan.model_validate(payload)


def _revision_event(
    revised: NpcPlan,
    expected_revision: int,
    context: SimulationContext,
    *,
    control_interruption: Literal["account_control"] | None = None,
    exploration_result: Literal["discovered", "nothing_hidden_found", "search_failed", "interrupted"] | None = None,
    trade_result: str | None = None,
    training_result: str | None = None,
    production_result: str | None = None,
) -> NpcPlanRevisedEvent:
    return NpcPlanRevisedEvent(
        event_id=npc_plan_event_id(
            context.command_id,
            context.ruleset_version,
            "revised",
            revised.plan_id,
            revised.revision,
        ),
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        actor_id=revised.actor_id,
        ruleset_version=context.ruleset_version,
        plan=revised,
        expected_revision=expected_revision,
        control_interruption=control_interruption,
        exploration_result=exploration_result,
        trade_result=trade_result,
        training_result=training_result,
        production_result=production_result,
    )


def _validated_transition(
    state: WorldState,
    context: SimulationContext,
    before_hash: str,
    source_plan: NpcPlan,
    proposed_events: list[DomainEvent],
    *,
    outcome: NpcPlanExecutionOutcome,
    next_due_tick: int | None = None,
    required_ticks: int | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
    consumed_step_index: int | None = None,
    evidence_event_ids: tuple[UUID, ...] = (),
    journey_id: UUID | None = None,
    arrived_at: UUID | None = None,
    preflight=None,
    rule_resolution: RuleResolution | None = None,
) -> NpcPlanTransition:
    projected = state
    seen_ids: set[UUID] = set()
    try:
        for index, event in enumerate(proposed_events):
            if (
                consumed_step_index is not None and evidence_event_ids
                and isinstance(event, NpcPlanRevisedEvent) and event.plan.status in {"ready", "completed"}
            ):
                candidates = [
                    goal for goal in projected.goals
                    if goal.id in event.plan.goal_ids and goal.status == "active"
                    and npc_goal_fulfillment_enabled(state, goal, plan_completed=event.plan.status == "completed")
                    and (not npc_goal_conditions_met(state, goal) or (
                        arrived_at is not None and any(
                            condition.kind == "reach_location" and condition.target_id == arrived_at
                            for condition in goal.conditions
                        )
                    ))
                ]
                fulfilled = frozenset(goal.id for goal in candidates if npc_goal_conditions_met(projected, goal))
                if fulfilled and state.metadata.get(NPC_GOAL_DEPENDENCY_COMPLETION_POLICY) is True:
                    fulfilled = npc_goal_fulfillment_closure(projected, candidates, fulfilled)
                if fulfilled:
                    event = event.model_copy(update={"fulfilled_goal_ids": tuple(sorted(fulfilled, key=str))})
                    proposed_events[index] = event
            if isinstance(event, NpcPlanRevisedEvent) and consumed_step_index is not None:
                followup = build_npc_goal_followup_review(projected, context.world_id, event.plan, event.fulfilled_goal_ids)
                if followup is not None:
                    event = event.model_copy(update={"goal_followup_review_tick": followup.due_tick})
                    proposed_events[index] = event
            event_id = getattr(event, "event_id", None)
            if isinstance(event_id, UUID):
                if event_id in seen_ids:
                    raise ValueError("duplicate event id in proposal")
                seen_ids.add(event_id)
            if not _event_matches_context(event, context):
                raise ValueError("event scope does not match context")
            projected = apply_event(projected, event)
    except Exception:
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=source_plan,
            reason_code="npc_event_batch_rejected_by_reducer",
            required_ticks=required_ticks,
            journey_id=journey_id,
            preflight=preflight,
        )
    after_input_hash = state_hash(state)
    if (
        after_input_hash != before_hash
        or projected.current_tick != state.current_tick
    ):
        return _empty_transition(
            state, context, before_hash, outcome="invalid_proposal", plan=source_plan,
            reason_code="npc_transition_mutated_input_or_advanced_time",
            journey_id=journey_id,
            preflight=preflight,
        )
    projected_plan = next(
        (plan for plan in projected.npc_plans if plan.plan_id == source_plan.plan_id),
        source_plan,
    )
    return NpcPlanTransition(
        outcome=outcome,
        events=tuple(proposed_events),
        next_due_tick=next_due_tick,
        required_ticks=required_ticks,
        reason_code=reason_code,
        reason=reason,
        plan_id=source_plan.plan_id,
        actor_id=source_plan.actor_id,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        state_version=context.state_version,
        tick_before=state.current_tick,
        tick_after=projected.current_tick,
        source_state_hash=before_hash,
        input_state_hash_after=after_input_hash,
        projected_state_hash=state_hash(projected),
        source_revision=source_plan.revision,
        result_revision=projected_plan.revision,
        source_current_step=source_plan.current_step,
        result_current_step=projected_plan.current_step,
        consumed_step_index=consumed_step_index,
        evidence_event_ids=evidence_event_ids,
        journey_id=journey_id,
        capability_id=getattr(preflight, "capability_id", None),
        capability_registry_version=getattr(preflight, "registry_version", None),
        action_kind=getattr(preflight, "action_kind", None),
        rule_resolution=rule_resolution if rule_resolution is not None else getattr(preflight, "resolution", None),
    )


def _event_matches_context(event: DomainEvent, context: SimulationContext) -> bool:
    for name, expected in (
        ("world_id", context.world_id),
        ("branch_id", context.branch_id),
        ("command_id", context.command_id),
        ("ruleset_version", context.ruleset_version),
    ):
        value = getattr(event, name, None)
        if value is not None and value != expected:
            return False
    return True


def _empty_transition(
    state: WorldState,
    context: SimulationContext,
    before_hash: str,
    *,
    outcome: NpcPlanExecutionOutcome,
    plan: NpcPlan | None = None,
    plan_id: UUID | None = None,
    actor_id: UUID | None = None,
    next_due_tick: int | None = None,
    required_ticks: int | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
    journey_id: UUID | None = None,
    preflight=None,
) -> NpcPlanTransition:
    after_hash = state_hash(state)
    resolved_plan_id = plan.plan_id if plan is not None else plan_id
    if resolved_plan_id is None:
        raise ValueError("An NPC plan transition requires a plan identifier")
    return NpcPlanTransition(
        outcome=outcome,
        events=(),
        next_due_tick=next_due_tick,
        required_ticks=required_ticks,
        reason_code=reason_code,
        reason=reason,
        plan_id=resolved_plan_id,
        actor_id=plan.actor_id if plan is not None else actor_id,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        state_version=context.state_version,
        tick_before=state.current_tick,
        tick_after=state.current_tick,
        source_state_hash=before_hash,
        input_state_hash_after=after_hash,
        projected_state_hash=before_hash,
        source_revision=plan.revision if plan is not None else None,
        result_revision=plan.revision if plan is not None else None,
        source_current_step=plan.current_step if plan is not None else None,
        result_current_step=plan.current_step if plan is not None else None,
        journey_id=journey_id,
        capability_id=getattr(preflight, "capability_id", None),
        capability_registry_version=getattr(preflight, "registry_version", None),
        action_kind=getattr(preflight, "action_kind", None),
        rule_resolution=getattr(preflight, "resolution", None),
    )
