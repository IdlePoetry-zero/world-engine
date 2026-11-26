"""Typed, deterministic strategy reviews for autonomous organizations.

Strategies state a descriptive mission and a finite set of conditional goals.
They do not predict a narrative or create resources: a qualifying review may
only move an already held resource between accounts owned by the same
organization. Review receipts are recomputed by the event reducer.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, model_validator

from .components import (
    GovernanceComponent,
    ResourceName,
    ScheduledEventRecord,
)

if TYPE_CHECKING:
    from .models import WorldState


ORGANIZATION_STRATEGY_REVIEW_HANDLER_ID = "organization.strategy_review"
ORGANIZATION_STRATEGY_REVIEW_HANDLER_VERSION = "1.0.0"
ORGANIZATION_STRATEGY_REVIEW_PHASE = "decision"
ORGANIZATION_STRATEGY_REVIEW_EVENT_TYPE = "core.organization_strategy_review_resolved.v1"

_ORGANIZATION_KINDS = frozenset({"organization", "polity", "government", "faction", "company"})
_MAX_REVIEWS = 100_000
_MAX_TICK = 2**63 - 1
_MAX_DECIMAL_SCALE = 6
_MAX_DECIMAL_INTEGER_DIGITS = 30


class StrategyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


def _exact_decimal(value: object) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("strategy quantities and thresholds must be decimal strings or integers")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("strategy quantities and thresholds must be finite decimals") from exc
    if not result.is_finite():
        raise ValueError("strategy quantities and thresholds must be finite decimals")
    parts = result.as_tuple()
    digits = parts.digits
    exponent = parts.exponent
    digit_count = len(digits)
    while digit_count and digits[digit_count - 1] == 0:
        digit_count -= 1
        exponent += 1
    scale = max(0, -exponent)
    integer_digits = max(1, digit_count + exponent)
    if scale > _MAX_DECIMAL_SCALE or integer_digits > _MAX_DECIMAL_INTEGER_DIGITS:
        raise ValueError("strategy decimals support at most 30 integer and 6 fractional digits")
    return result


StrategyDecimal = Annotated[Decimal, BeforeValidator(_exact_decimal)]


class StrategyCondition(StrategyModel):
    """One current-state condition used to decide whether a goal applies."""

    condition_id: ResourceName
    kind: Literal[
        "account_balance_at_least",
        "account_balance_below",
        "organization_status_is",
        "active_member_count_at_least",
    ]
    account_id: UUID | None = None
    threshold: StrategyDecimal | None = None
    expected_status: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def condition_shape_matches_kind(self) -> StrategyCondition:
        if self.kind in {"account_balance_at_least", "account_balance_below"}:
            if self.account_id is None or self.threshold is None or self.threshold < 0 or self.expected_status is not None:
                raise ValueError("account balance conditions require account_id and a nonnegative threshold only")
        elif self.kind == "organization_status_is":
            if self.account_id is not None or self.threshold is not None or self.expected_status is None:
                raise ValueError("organization status conditions require expected_status only")
        else:
            if (
                self.account_id is not None
                or self.threshold is None
                or self.threshold < 0
                or self.threshold != self.threshold.to_integral_value()
                or self.expected_status is not None
            ):
                raise ValueError("active member count conditions require a nonnegative integer threshold only")
        return self


class StrategyTransferAction(StrategyModel):
    """A fixed, exact transfer between two accounts owned by one organization."""

    kind: Literal["transfer_resource"] = "transfer_resource"
    from_account_id: UUID
    to_account_id: UUID
    resource_id: ResourceName
    quantity: StrategyDecimal = Field(gt=Decimal("0"))
    source_reserve_floor: StrategyDecimal = Field(default=Decimal("0"), ge=Decimal("0"))

    @model_validator(mode="after")
    def distinct_transfer_accounts(self) -> StrategyTransferAction:
        if self.from_account_id == self.to_account_id:
            raise ValueError("a strategy transfer requires two different accounts")
        with localcontext() as context:
            context.prec = 80
            _exact_decimal(self.quantity + self.source_reserve_floor)
        return self


class OrganizationalStrategyGoal(StrategyModel):
    """A ranked goal that applies only when every typed condition is true."""

    goal_id: ResourceName
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    priority: StrictInt = Field(default=50, ge=0, le=100)
    conditions: tuple[StrategyCondition, ...] = Field(min_length=1, max_length=32)
    action: StrategyTransferAction

    @model_validator(mode="after")
    def unique_conditions(self) -> OrganizationalStrategyGoal:
        ids = [condition.condition_id for condition in self.conditions]
        if len(ids) != len(set(ids)):
            raise ValueError("strategy goal condition IDs must be unique")
        return self


class OrganizationalStrategy(StrategyModel):
    """One organization’s recurring mission, review cadence, and goal set."""

    strategy_id: ResourceName
    organization_id: UUID
    mission: str = Field(min_length=1, max_length=2000)
    charter_clause_id: ResourceName
    authority_tag: ResourceName
    first_review_tick: Annotated[StrictInt, Field(ge=-(2**63), le=_MAX_TICK)]
    review_interval_ticks: Annotated[StrictInt, Field(gt=0, le=_MAX_TICK)]
    max_reviews: StrictInt = Field(default=_MAX_REVIEWS, ge=1, le=_MAX_REVIEWS)
    goals: tuple[OrganizationalStrategyGoal, ...] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def unique_goals(self) -> OrganizationalStrategy:
        ids = [goal.goal_id for goal in self.goals]
        if len(ids) != len(set(ids)):
            raise ValueError("strategy goal IDs must be unique")
        if self.first_review_tick + self.max_reviews * self.review_interval_ticks > _MAX_TICK:
            raise ValueError("the configured strategy review series exceeds the signed world-clock range")
        return self


class StrategyConditionEvaluation(StrategyModel):
    """The exact current value checked for one condition at formal review."""

    condition_id: ResourceName
    kind: Literal[
        "account_balance_at_least",
        "account_balance_below",
        "organization_status_is",
        "active_member_count_at_least",
    ]
    account_id: UUID | None = None
    threshold: StrategyDecimal | None = None
    expected_status: str | None = None
    observed_balance: StrategyDecimal | None = None
    observed_status: str | None = None
    observed_active_member_count: StrictInt | None = Field(default=None, ge=0)
    satisfied: bool


class StrategyGoalEvaluation(StrategyModel):
    """Condition and funding evidence captured for one candidate goal."""

    goal_id: ResourceName
    priority: StrictInt = Field(ge=0, le=100)
    conditions: tuple[StrategyConditionEvaluation, ...] = Field(min_length=1, max_length=32)
    conditions_satisfied: bool
    source_account_id: UUID
    observed_source_balance: StrategyDecimal
    required_source_balance: StrategyDecimal
    action_fundable: bool


class StrategyTransferReceipt(StrategyModel):
    """Actual before/after account balances for a successful internal transfer."""

    goal_id: ResourceName
    from_account_id: UUID
    to_account_id: UUID
    resource_id: ResourceName
    quantity: StrategyDecimal = Field(gt=Decimal("0"))
    source_balance_before: StrategyDecimal
    source_balance_after: StrategyDecimal
    target_balance_before: StrategyDecimal
    target_balance_after: StrategyDecimal


class OrganizationalStrategyReviewRecord(StrategyModel):
    """Latest formal review projection; the append-only event log retains history."""

    strategy_id: ResourceName
    organization_id: UUID
    review_index: StrictInt = Field(ge=1, le=_MAX_REVIEWS)
    reviewed_tick: Annotated[StrictInt, Field(ge=-(2**63), le=_MAX_TICK)]
    governance_revision: StrictInt | None = Field(default=None, ge=1)
    charter_clause_id: ResourceName
    charter_clause_present: bool
    authority_tag: ResourceName
    organization_authority_present: bool
    authorized_actor_id: UUID | None = None
    reason: Literal[
        "action_executed",
        "organization_inactive",
        "no_current_authority",
        "no_conditions_met",
        "insufficient_resources",
        "action_unavailable",
    ]
    goal_evaluations: tuple[StrategyGoalEvaluation, ...] = Field(min_length=2, max_length=64)
    selected_goal_id: ResourceName | None = None
    transfer: StrategyTransferReceipt | None = None

    @model_validator(mode="after")
    def action_matches_review_reason(self) -> OrganizationalStrategyReviewRecord:
        if self.reason == "action_executed":
            if self.selected_goal_id is None or self.transfer is None or self.authorized_actor_id is None:
                raise ValueError("an executed strategy review requires authority, a selected goal, and a transfer receipt")
            if self.transfer.goal_id != self.selected_goal_id:
                raise ValueError("strategy review transfer must belong to its selected goal")
        elif self.selected_goal_id is not None or self.transfer is not None:
            raise ValueError("an abstaining strategy review cannot contain an action")
        if self.reason == "no_current_authority" and self.authorized_actor_id is not None:
            raise ValueError("a strategy review without current authority cannot record an authorized actor")
        return self


def organization_strategy_schedule_id(strategy: OrganizationalStrategy) -> UUID:
    """Return the stable schedule ID for a strategy profile."""

    if not isinstance(strategy, OrganizationalStrategy):
        raise ValueError("strategy schedule IDs require an OrganizationalStrategy profile")
    return uuid5(strategy.organization_id, f"organization.strategy_review.v1:{strategy.strategy_id}")


def organization_strategy_review_event_id(schedule_id: UUID, review_index: int) -> UUID:
    """Return a retry-stable event ID for one scheduled review occurrence."""

    if not isinstance(schedule_id, UUID) or type(review_index) is not int or not 1 <= review_index <= _MAX_REVIEWS:
        raise ValueError("strategy review event IDs require a schedule UUID and positive bounded review index")
    return uuid5(schedule_id, f"review.v1:{review_index}")


def build_organization_strategy_schedule(strategy: OrganizationalStrategy) -> ScheduledEventRecord:
    """Build the finite, recurring formal review schedule for a profile."""

    return ScheduledEventRecord(
        id=organization_strategy_schedule_id(strategy),
        due_tick=strategy.first_review_tick,
        phase=ORGANIZATION_STRATEGY_REVIEW_PHASE,
        priority=0,
        handler_id=ORGANIZATION_STRATEGY_REVIEW_HANDLER_ID,
        handler_version=ORGANIZATION_STRATEGY_REVIEW_HANDLER_VERSION,
        repeat_ticks=strategy.review_interval_ticks,
        max_runs=strategy.max_reviews,
        payload={
            "organization_id": str(strategy.organization_id),
            "strategy_id": strategy.strategy_id,
        },
    )


def install_organizational_strategy(
    state: WorldState,
    strategy: OrganizationalStrategy,
) -> WorldState:
    """Install a strategy and its recurring schedule into a new world state.

    This is a world-genesis helper for pack integration. Runtime strategy
    changes remain event-based; callers should not use this to edit a live
    saved world.
    """

    if not isinstance(strategy, OrganizationalStrategy):
        raise ValueError("strategy installation requires an OrganizationalStrategy profile")
    if strategy.first_review_tick < state.current_tick:
        raise ValueError("the first strategy review cannot precede the initial world clock")
    if strategy.organization_id in state.organizational_strategies:
        raise ValueError("an organization can have only one autonomous strategy profile")
    schedule = build_organization_strategy_schedule(strategy)
    if any(row.id == schedule.id for row in state.scheduled_events):
        raise ValueError("the strategy review schedule ID is already in use")
    validate_strategy_profile_state(state, strategy)
    return state.model_copy(update={
        "organizational_strategies": {
            **state.organizational_strategies,
            strategy.organization_id: strategy,
        },
        "scheduled_events": [*state.scheduled_events, schedule],
    })


def validate_strategy_profile_state(state: WorldState, strategy: OrganizationalStrategy) -> None:
    """Validate the organization and exact holder accounts referenced by a profile."""

    organization = state.entities.get(strategy.organization_id)
    if organization is None or organization.kind not in _ORGANIZATION_KINDS:
        raise ValueError("strategy must reference an existing organization, polity, government, faction, or company")
    for goal in strategy.goals:
        action = goal.action
        source = state.resources.get(action.from_account_id)
        target = state.resources.get(action.to_account_id)
        if (
            source is None
            or target is None
            or source.owner_id != organization.id
            or target.owner_id != organization.id
            or source.account_type != "holder"
            or target.account_type != "holder"
            or source.resource_id != action.resource_id
            or target.resource_id != action.resource_id
        ):
            raise ValueError("strategy transfers require existing same-resource holder accounts owned by the organization")
        for condition in goal.conditions:
            if condition.account_id is None:
                continue
            account = state.resources.get(condition.account_id)
            if (
                account is None
                or account.owner_id != organization.id
                or account.account_type != "holder"
            ):
                raise ValueError("strategy account conditions must reference existing holder accounts owned by the organization")


def evaluate_organizational_strategy_review(
    state: WorldState,
    strategy: OrganizationalStrategy,
    review_index: int,
) -> OrganizationalStrategyReviewRecord:
    """Recompute live authority, conditions and the one permitted transfer."""

    if type(review_index) is not int or not 1 <= review_index <= strategy.max_reviews:
        raise ValueError("strategy review index is outside its configured finite series")
    expected_tick = strategy.first_review_tick + (review_index - 1) * strategy.review_interval_ticks
    if state.current_tick != expected_tick:
        raise ValueError("strategy reviews resolve only at their exact scheduled world-time boundary")

    organization = state.entities.get(strategy.organization_id)
    governance: GovernanceComponent | None = None
    if organization is not None and organization.components.get("governance") is not None:
        try:
            governance = GovernanceComponent.model_validate(organization.components["governance"])
        except Exception as exc:
            raise ValueError("strategy organization has an invalid governance component") from exc

    governance_revision = governance.revision if governance is not None else None
    clause_present = governance is not None and strategy.charter_clause_id in governance.charter_clause_ids
    organization_authority_present = governance is not None and strategy.authority_tag in governance.authority_tags
    authorized_actor_id = _strategy_authorized_actor(state, strategy, governance) if governance is not None else None

    evaluations: list[StrategyGoalEvaluation] = []
    for goal in sorted(strategy.goals, key=lambda item: (-item.priority, item.goal_id)):
        condition_evaluations = tuple(
            _evaluate_strategy_condition(state, strategy, condition)
            for condition in goal.conditions
        )
        conditions_satisfied = all(condition.satisfied for condition in condition_evaluations)
        source = state.resources.get(goal.action.from_account_id)
        target = state.resources.get(goal.action.to_account_id)
        source_balance = source.balance if source is not None else Decimal("0")
        with localcontext() as context:
            context.prec = 80
            required_source_balance = goal.action.quantity + goal.action.source_reserve_floor
            action_fundable = source is not None and source_balance >= required_source_balance
            if action_fundable and target is not None:
                try:
                    _exact_decimal(source_balance - goal.action.quantity)
                    _exact_decimal(target.balance + goal.action.quantity)
                except ValueError:
                    action_fundable = False
            else:
                action_fundable = False
        evaluations.append(StrategyGoalEvaluation(
            goal_id=goal.goal_id,
            priority=goal.priority,
            conditions=condition_evaluations,
            conditions_satisfied=conditions_satisfied,
            source_account_id=goal.action.from_account_id,
            observed_source_balance=source_balance,
            required_source_balance=required_source_balance,
            action_fundable=action_fundable,
        ))

    reason: Literal[
        "action_executed",
        "organization_inactive",
        "no_current_authority",
        "no_conditions_met",
        "insufficient_resources",
        "action_unavailable",
    ]
    selected_goal_id: str | None = None
    receipt: StrategyTransferReceipt | None = None
    if organization is None or organization.status != "active":
        reason = "organization_inactive"
        authorized_actor_id = None
    elif not clause_present or not organization_authority_present or authorized_actor_id is None:
        reason = "no_current_authority"
        authorized_actor_id = None
    else:
        evaluated_by_goal = {item.goal_id: item for item in evaluations}
        eligible = [
            goal for goal in sorted(strategy.goals, key=lambda item: (-item.priority, item.goal_id))
            if evaluated_by_goal[goal.goal_id].conditions_satisfied
        ]
        funded = [goal for goal in eligible if evaluated_by_goal[goal.goal_id].action_fundable]
        if not eligible:
            reason = "no_conditions_met"
        elif not funded:
            source_fundable = any(
                evaluated_by_goal[goal.goal_id].observed_source_balance
                >= evaluated_by_goal[goal.goal_id].required_source_balance
                for goal in eligible
            )
            reason = "action_unavailable" if source_fundable else "insufficient_resources"
        else:
            chosen = funded[0]
            source = state.resources[chosen.action.from_account_id]
            target = state.resources[chosen.action.to_account_id]
            quantity = chosen.action.quantity
            with localcontext() as context:
                context.prec = 80
                source_after = source.balance - quantity
                target_after = target.balance + quantity
            try:
                _exact_decimal(source_after)
                _exact_decimal(target_after)
            except ValueError:
                return OrganizationalStrategyReviewRecord(
                    strategy_id=strategy.strategy_id,
                    organization_id=strategy.organization_id,
                    review_index=review_index,
                    reviewed_tick=state.current_tick,
                    governance_revision=governance_revision,
                    charter_clause_id=strategy.charter_clause_id,
                    charter_clause_present=clause_present,
                    authority_tag=strategy.authority_tag,
                    organization_authority_present=organization_authority_present,
                    authorized_actor_id=authorized_actor_id,
                    reason="action_unavailable",
                    goal_evaluations=tuple(evaluations),
                )
            selected_goal_id = chosen.goal_id
            receipt = StrategyTransferReceipt(
                goal_id=chosen.goal_id,
                from_account_id=chosen.action.from_account_id,
                to_account_id=chosen.action.to_account_id,
                resource_id=chosen.action.resource_id,
                quantity=quantity,
                source_balance_before=source.balance,
                source_balance_after=source_after,
                target_balance_before=target.balance,
                target_balance_after=target_after,
            )
            reason = "action_executed"

    return OrganizationalStrategyReviewRecord(
        strategy_id=strategy.strategy_id,
        organization_id=strategy.organization_id,
        review_index=review_index,
        reviewed_tick=state.current_tick,
        governance_revision=governance_revision,
        charter_clause_id=strategy.charter_clause_id,
        charter_clause_present=clause_present,
        authority_tag=strategy.authority_tag,
        organization_authority_present=organization_authority_present,
        authorized_actor_id=authorized_actor_id,
        reason=reason,
        goal_evaluations=tuple(evaluations),
        selected_goal_id=selected_goal_id,
        transfer=receipt,
    )


def _strategy_authorized_actor(
    state: WorldState,
    strategy: OrganizationalStrategy,
    governance: GovernanceComponent,
) -> UUID | None:
    if (
        strategy.authority_tag not in governance.authority_tags
        or strategy.charter_clause_id not in governance.charter_clause_ids
    ):
        return None
    candidate_ids = sorted(
        {
            office.holder_id
            for office in governance.offices
            if office.holder_id is not None and strategy.authority_tag in office.authority
        },
        key=str,
    )
    for candidate_id in candidate_ids:
        if candidate_id not in governance.members:
            continue
        candidate = state.entities.get(candidate_id)
        if candidate is not None and candidate.kind in {"character", "person"} and candidate.status == "active":
            return candidate_id
    return None


def _evaluate_strategy_condition(
    state: WorldState,
    strategy: OrganizationalStrategy,
    condition: StrategyCondition,
) -> StrategyConditionEvaluation:
    common = {
        "condition_id": condition.condition_id,
        "kind": condition.kind,
        "account_id": condition.account_id,
        "threshold": condition.threshold,
        "expected_status": condition.expected_status,
    }
    if condition.kind in {"account_balance_at_least", "account_balance_below"}:
        account = state.resources.get(condition.account_id)
        observed_balance = account.balance if account is not None else None
        met = False
        if observed_balance is not None and condition.threshold is not None:
            met = (
                observed_balance >= condition.threshold
                if condition.kind == "account_balance_at_least"
                else observed_balance < condition.threshold
            )
        return StrategyConditionEvaluation(**common, observed_balance=observed_balance, satisfied=met)
    organization = state.entities.get(strategy.organization_id)
    if condition.kind == "organization_status_is":
        observed_status = organization.status if organization is not None else None
        return StrategyConditionEvaluation(
            **common,
            observed_status=observed_status,
            satisfied=observed_status == condition.expected_status,
        )
    governance = None
    if organization is not None and organization.components.get("governance") is not None:
        try:
            governance = GovernanceComponent.model_validate(organization.components["governance"])
        except Exception:
            governance = None
    count = 0
    if governance is not None:
        count = sum(
            1
            for member_id in governance.members
            if (member := state.entities.get(member_id)) is not None
            and member.kind in {"character", "person"}
            and member.status == "active"
        )
    threshold = condition.threshold or Decimal("0")
    return StrategyConditionEvaluation(
        **common,
        observed_active_member_count=count,
        satisfied=Decimal(count) >= threshold,
    )


def validate_organization_strategy_state(state: WorldState) -> None:
    """Validate profiles, repeating schedules, and latest review projections."""

    strategy_by_schedule: dict[UUID, OrganizationalStrategy] = {}
    for organization_id, raw_strategy in state.organizational_strategies.items():
        strategy = OrganizationalStrategy.model_validate(raw_strategy.model_dump(mode="python"))
        if organization_id != strategy.organization_id:
            raise ValueError("strategy map key must match its organization ID")
        validate_strategy_profile_state(state, strategy)
        schedule_id = organization_strategy_schedule_id(strategy)
        if schedule_id in strategy_by_schedule:
            raise ValueError("organization strategy schedule IDs must be unique")
        strategy_by_schedule[schedule_id] = strategy

    strategy_schedules = {
        row.id: row
        for row in state.scheduled_events
        if row.handler_id == ORGANIZATION_STRATEGY_REVIEW_HANDLER_ID
    }
    if set(strategy_schedules) != set(strategy_by_schedule):
        raise ValueError("each strategy profile must have exactly one registered recurring review schedule")
    for schedule_id, strategy in strategy_by_schedule.items():
        schedule = strategy_schedules[schedule_id]
        expected_schedule = build_organization_strategy_schedule(strategy)
        if (
            schedule.handler_version != expected_schedule.handler_version
            or schedule.phase != expected_schedule.phase
            or schedule.priority != expected_schedule.priority
            or schedule.repeat_ticks != expected_schedule.repeat_ticks
            or schedule.max_runs != expected_schedule.max_runs
            or schedule.payload != expected_schedule.payload
            or schedule.completed_runs > strategy.max_reviews
            or schedule.revision != schedule.completed_runs + 1
            or schedule.due_tick != strategy.first_review_tick + schedule.completed_runs * strategy.review_interval_ticks
            or schedule.status != ("queued" if schedule.completed_runs < strategy.max_reviews else "completed")
            or schedule.status == "queued" and schedule.due_tick < state.current_tick
        ):
            raise ValueError("strategy schedule does not match its profile and committed review count")
        latest_raw = state.organizational_strategy_reviews.get(strategy.organization_id)
        latest = (
            OrganizationalStrategyReviewRecord.model_validate(latest_raw.model_dump(mode="python"))
            if latest_raw is not None else None
        )
        if schedule.completed_runs == 0:
            if latest is not None:
                raise ValueError("an unreviewed strategy cannot have a latest review receipt")
        elif (
            latest is None
            or latest.strategy_id != strategy.strategy_id
            or latest.organization_id != strategy.organization_id
            or latest.review_index != schedule.completed_runs
            or latest.reviewed_tick != strategy.first_review_tick + (schedule.completed_runs - 1) * strategy.review_interval_ticks
            or latest.reviewed_tick > state.current_tick
        ):
            raise ValueError("latest strategy review receipt must match the recurring schedule")
    for organization_id in state.organizational_strategy_reviews:
        if organization_id not in state.organizational_strategies:
            raise ValueError("strategy review receipts cannot outlive their strategy profile")
