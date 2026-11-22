"""Stable ordinary NPC policy, separate from consequential model decisions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.domain import InventoryComponent, NeedsComponent, WorldState

from .knowledge import AgentKnowledge, build_agent_knowledge

Activity = Literal["wait", "rest", "eat", "drink", "travel", "work", "study", "produce", "socialize"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PolicyCandidate(_FrozenModel):
    activity: Activity
    score: int = Field(ge=0)
    reason_code: str = Field(min_length=1, max_length=80)
    evidence_ids: tuple[UUID, ...] = ()


class AgentDecision(_FrozenModel):
    actor_id: UUID
    activity: Activity
    reason_code: str = Field(min_length=1, max_length=80)
    candidates: tuple[PolicyCandidate, ...]
    knowledge: AgentKnowledge


class BackgroundSelfCareDecision(_FrozenModel):
    """One deterministic low-impact choice for an NPC during a background window."""

    actor_id: UUID
    activity: Literal["eat", "drink", "rest"]
    reason_code: str = Field(min_length=1, max_length=80)
    resource_id: str | None = None
    account_id: UUID | None = None
    quantity: Decimal | None = Field(default=None, gt=Decimal("0"))

    @model_validator(mode="after")
    def matching_food_stock(self) -> BackgroundSelfCareDecision:
        if self.activity in {"eat", "drink"}:
            if self.resource_id is None or self.account_id is None or self.quantity is None:
                raise ValueError("eating or drinking choices require a resource, account, and positive quantity")
        elif any(value is not None for value in (self.resource_id, self.account_id, self.quantity)):
            raise ValueError("resting background choices cannot cite resource stock")
        return self


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """A deterministic policy for routine and immediate self-care choices.

    It only chooses between actions the simulation can resolve with registered
    rules. Goals with tied top priority are surfaced to the decision layer instead
    of being broken by an arbitrary tie-break here.
    """

    rest_when_below: int = 30
    eat_when_below: int = 25

    def choose(
        self,
        state: WorldState,
        actor_id: UUID,
        requested_activity: Activity,
        *,
        food_available: bool,
        water_available: bool = False,
    ) -> AgentDecision:
        actor = state.entities.get(actor_id)
        if actor is None or actor.kind not in {"character", "person"}:
            raise ValueError(f"Agent {actor_id} is not an existing character.")
        knowledge = build_agent_knowledge(state, actor_id)
        needs_raw = actor.components.get("needs")
        needs = NeedsComponent.model_validate(needs_raw) if needs_raw is not None else NeedsComponent()

        candidates: list[PolicyCandidate] = []
        food_threshold = min(self.eat_when_below, needs.food.critical_below) if needs.food is not None else self.eat_when_below
        rest_threshold = min(self.rest_when_below, needs.rest.critical_below) if needs.rest is not None else self.rest_when_below
        if needs.food is not None and needs.food.value <= food_threshold and food_available:
            candidates.append(PolicyCandidate(activity="eat", score=100 + (food_threshold - needs.food.value), reason_code="critical_food_need"))
        water_threshold = min(
            self.eat_when_below,
            needs.water.critical_below if needs.water is not None else self.eat_when_below,
        )
        if needs.water is not None and needs.water.value <= water_threshold and water_available:
            candidates.append(PolicyCandidate(activity="drink", score=120 + (water_threshold - needs.water.value), reason_code="critical_water_need"))
        if needs.rest is not None and needs.rest.value <= rest_threshold:
            candidates.append(PolicyCandidate(activity="rest", score=80 + (rest_threshold - needs.rest.value), reason_code="critical_rest_need"))

        active_goals = sorted(knowledge.active_goals, key=lambda goal: (-goal.priority, str(goal.id)))
        if (
            requested_activity in {"work", "study", "produce", "socialize"}
            and not candidates
            and len(active_goals) > 1
            and active_goals[0].priority == active_goals[1].priority
        ):
            raise AmbiguousGoalPriority(actor_id, tuple(goal.id for goal in active_goals if goal.priority == active_goals[0].priority))

        candidates.append(
            PolicyCandidate(
                activity=requested_activity,
                score=10,
                reason_code="continuing_authorized_plan",
                evidence_ids=tuple(goal.id for goal in active_goals[:1]),
            )
        )
        candidates.sort(key=lambda candidate: (-candidate.score, candidate.activity))
        selected = candidates[0]
        return AgentDecision(
            actor_id=actor_id,
            activity=selected.activity,
            reason_code=selected.reason_code,
            candidates=tuple(candidates),
            knowledge=knowledge,
        )

    def choose_background(
        self,
        state: WorldState,
        *,
        food_resource_id: str | None = None,
        exclude_actor_ids: frozenset[UUID] = frozenset(),
    ) -> tuple[BackgroundSelfCareDecision, ...]:
        """Choose routine food/rest actions for eligible nonacting characters.

        Only characters whose typed needs crossed the policy threshold incur a
        knowledge lookup. Food is available only when a matching unexpired lot
        is backed by an actor-owned resource account. This method proposes
        ordinary actions; the RuleEngine must still consume ledgers and apply
        needs through its registered event path.
        """
        roles = state.metadata.get("resource_roles")
        if food_resource_id is None:
            if isinstance(roles, dict) and roles.get("food_resource_id"):
                food_resource_id = str(roles["food_resource_id"])
        water_resource_id = (
            str(roles["water_resource_id"])
            if isinstance(roles, dict) and roles.get("water_resource_id")
            else None
        )

        decisions: list[BackgroundSelfCareDecision] = []
        for actor in sorted(state.entities.values(), key=lambda entity: str(entity.id)):
            if (
                actor.id in exclude_actor_ids
                or actor.kind not in {"character", "person"}
                or actor.status in {"dead", "retired"}
            ):
                continue
            raw_needs = actor.components.get("needs")
            if raw_needs is None:
                continue
            needs = NeedsComponent.model_validate(raw_needs)
            food_threshold = min(
                self.eat_when_below,
                needs.food.critical_below if needs.food is not None else self.eat_when_below,
            )
            rest_threshold = min(
                self.rest_when_below,
                needs.rest.critical_below if needs.rest is not None else self.rest_when_below,
            )
            water_threshold = min(
                self.eat_when_below,
                needs.water.critical_below if needs.water is not None else self.eat_when_below,
            )
            if not (
                (needs.food is not None and needs.food.value <= food_threshold)
                or (needs.water is not None and needs.water.value <= water_threshold)
                or (needs.rest is not None and needs.rest.value <= rest_threshold)
            ):
                continue

            food_account_id, food_available = self._first_stock(
                state, actor.id, food_resource_id
            )
            water_account_id, water_available = self._first_stock(
                state, actor.id, water_resource_id
            )
            policy = self.choose(
                state,
                actor.id,
                "wait",
                food_available=food_account_id is not None,
                water_available=water_account_id is not None,
            )
            if policy.activity == "eat" and food_account_id is not None:
                assert food_available is not None
                wanted = Decimal("1")
                quantity = min(wanted, food_available)
                if quantity > 0:
                    decisions.append(
                        BackgroundSelfCareDecision(
                            actor_id=actor.id,
                            activity="eat",
                            reason_code=policy.reason_code,
                            resource_id=food_resource_id,
                            account_id=food_account_id,
                            quantity=quantity,
                        )
                    )
            elif policy.activity == "drink" and water_account_id is not None:
                assert water_available is not None
                quantity = min(Decimal("1"), water_available)
                if quantity > 0:
                    decisions.append(
                        BackgroundSelfCareDecision(
                            actor_id=actor.id,
                            activity="drink",
                            reason_code=policy.reason_code,
                            resource_id=water_resource_id,
                            account_id=water_account_id,
                            quantity=quantity,
                        )
                    )
            elif policy.activity == "rest":
                decisions.append(
                    BackgroundSelfCareDecision(
                        actor_id=actor.id,
                        activity="rest",
                        reason_code=policy.reason_code,
                    )
                )
        return tuple(decisions)

    def next_background_self_care_boundary(
        self,
        state: WorldState,
        *,
        exclude_actor_ids: frozenset[UUID] = frozenset(),
    ) -> int | None:
        """Find the earliest exact tick when any eligible agent needs routine care."""
        roles = state.metadata.get("resource_roles")
        food_resource_id = (
            str(roles["food_resource_id"])
            if isinstance(roles, dict) and roles.get("food_resource_id")
            else None
        )
        water_resource_id = (
            str(roles["water_resource_id"])
            if isinstance(roles, dict) and roles.get("water_resource_id")
            else None
        )
        candidates: list[int] = []
        day_seconds = state.calendar.day_seconds
        for actor in state.entities.values():
            if (
                actor.id in exclude_actor_ids
                or actor.kind not in {"character", "person"}
                or actor.status in {"dead", "retired"}
            ):
                continue
            raw_needs = actor.components.get("needs")
            if raw_needs is None:
                continue
            needs = NeedsComponent.model_validate(raw_needs)
            if needs.food is not None and food_resource_id is not None:
                account_id, _available = self._first_stock(state, actor.id, food_resource_id)
                if account_id is not None:
                    threshold = min(self.eat_when_below, needs.food.critical_below)
                    candidates.append(
                        self._need_boundary(
                            needs.food.value,
                            threshold,
                            needs.food.decay_remainder,
                            rate_per_day=18,
                            current_tick=state.current_tick,
                            day_seconds=day_seconds,
                        )
                    )
            if needs.water is not None and water_resource_id is not None:
                account_id, _available = self._first_stock(state, actor.id, water_resource_id)
                if account_id is not None:
                    threshold = min(self.eat_when_below, needs.water.critical_below)
                    candidates.append(
                        self._need_boundary(
                            needs.water.value,
                            threshold,
                            needs.water.decay_remainder,
                            rate_per_day=30,
                            current_tick=state.current_tick,
                            day_seconds=day_seconds,
                        )
                    )
            if needs.rest is not None:
                threshold = min(self.rest_when_below, needs.rest.critical_below)
                candidates.append(
                    self._need_boundary(
                        needs.rest.value,
                        threshold,
                        needs.rest.decay_remainder,
                        rate_per_day=24,
                        current_tick=state.current_tick,
                        day_seconds=day_seconds,
                    )
                )
        return min(candidates) if candidates else None

    @staticmethod
    def _first_stock(
        state: WorldState,
        actor_id: UUID,
        resource_id: str | None,
    ) -> tuple[UUID | None, Decimal | None]:
        if resource_id is None:
            return None, None
        actor = state.entities[actor_id]
        raw_inventory = actor.components.get("inventory")
        if raw_inventory is None:
            return None, None
        inventory = InventoryComponent.model_validate(raw_inventory)
        for lot in inventory.lots:
            account = state.resources.get(lot.account_id)
            if (
                lot.resource_id == resource_id
                and lot.quantity > 0
                and account is not None
                and account.owner_id == actor_id
                and account.resource_id == resource_id
                and account.balance > 0
                and (lot.expiry_tick is None or lot.expiry_tick > state.current_tick)
            ):
                return lot.account_id, min(lot.quantity, account.balance)
        return None, None

    @staticmethod
    def _need_boundary(
        value: int,
        threshold: int,
        remainder: int,
        *,
        rate_per_day: int,
        current_tick: int,
        day_seconds: int,
    ) -> int:
        if value <= threshold:
            return current_tick
        points_to_drop = value - threshold
        numerator = points_to_drop * day_seconds - remainder
        duration = max(1, (numerator + rate_per_day - 1) // rate_per_day)
        return min(2**63 - 1, current_tick + duration)


class AmbiguousGoalPriority(ValueError):
    """An equal-priority goal conflict that requires a consequential choice."""

    def __init__(self, actor_id: UUID, goal_ids: tuple[UUID, ...]) -> None:
        self.actor_id = actor_id
        self.goal_ids = goal_ids
        super().__init__(f"Agent {actor_id} has multiple active top-priority goals.")
