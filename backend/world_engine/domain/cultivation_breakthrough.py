"""Pure, command-seeded planning for one cultivation breakthrough attempt.

This module deliberately stops at an immutable plan. An application layer must
resolve a registered policy before calling it and later bind the returned
expectations to the corresponding world events and reducers.
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR, localcontext
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import (
    CapabilitiesComponent,
    ExactDecimal,
    HealthComponent,
    InventoryComponent,
    ProgressionComponent,
    ResourceName,
    SkillsComponent,
)
from .inventory import FifoLotAllocation, allocate_fifo_lots, reduce_fifo_inventory
if TYPE_CHECKING:
    from .models import ResourceAccount, WorldState


CULTIVATION_BREAKTHROUGH_POLICY = "cultivation.breakthrough.v1"
CULTIVATION_BREAKTHROUGH_ROLL_LABEL = "cultivation.breakthrough"
CULTIVATION_QI_RESOURCE_ID = "cultivation.qi"
CULTIVATION_PRACTICE_SKILL_ID = "skill.cultivation_practice"
CULTIVATION_PRACTICE_CAPABILITY = "cultivation.practice"
CULTIVATION_REALM_MILESTONE_PREFIX = "cultivation.realm:"
CULTIVATION_FAILURE_INJURY_TYPE = "core.injury.strain"

_MAX_REVISION = 2**63 - 1
_MAX_WORLD_TICK = 2**63 - 1
_MAX_BREAKTHROUGH_DURATION = 2**31 - 1
_MAX_FAILURE_DAMAGE = 1_000_000
_REALM_ID_MAX_LENGTH = 160 - len(CULTIVATION_REALM_MILESTONE_PREFIX)


class CultivationBreakthroughError(ValueError):
    """A stable, framework-independent reason that an attempt cannot be planned."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _BreakthroughModel(BaseModel):
    """Base for immutable, closed cultivation plan and policy contracts."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True,
        validate_assignment=True, allow_inf_nan=False,
    )


class CultivationBreakthroughPolicy(_BreakthroughModel):
    """Server-registered, bounded terms for one adjacent realm transition.

    ``initial_realm`` is the explicit fallback when the actor has no prior
    ``cultivation.realm:`` milestone. The model never looks up or invents
    policies from generated text.
    """

    policy_id: ResourceName
    policy_version: Literal["cultivation.breakthrough.v1"] = CULTIVATION_BREAKTHROUGH_POLICY
    initial_realm: str = Field(min_length=1, max_length=_REALM_ID_MAX_LENGTH, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    source_realm: str = Field(min_length=1, max_length=_REALM_ID_MAX_LENGTH, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    target_realm: str = Field(min_length=1, max_length=_REALM_ID_MAX_LENGTH, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    qi_resource_id: Literal["cultivation.qi"] = CULTIVATION_QI_RESOURCE_ID
    qi_cost: ExactDecimal = Field(gt=Decimal("0"))
    duration_ticks: StrictInt = Field(gt=0, le=_MAX_BREAKTHROUGH_DURATION)
    minimum_practice_skill_level: ExactDecimal = Field(ge=Decimal("0"), le=Decimal("100"))
    base_chance_percent: StrictInt = Field(ge=0, le=100)
    skill_bonus_per_point: StrictInt = Field(default=1, ge=0, le=100)
    maximum_skill_bonus: StrictInt = Field(default=50, ge=0, le=100)
    failure_damage: StrictInt = Field(gt=0, le=_MAX_FAILURE_DAMAGE)

    @model_validator(mode="after")
    def adjacent_distinct_realms(self) -> CultivationBreakthroughPolicy:
        if self.source_realm == self.target_realm:
            raise ValueError("a breakthrough must advance to a different realm")
        return self


_LONG_LIFE_POLICIES = (
    CultivationBreakthroughPolicy(
        policy_id="cultivation.qi_gathering.v1", initial_realm="mortal", source_realm="mortal",
        target_realm="qi_gathering", qi_cost="1", duration_ticks=3600,
        minimum_practice_skill_level="10", base_chance_percent=40, maximum_skill_bonus=25,
        failure_damage=4,
    ),
    CultivationBreakthroughPolicy(
        policy_id="cultivation.foundation.v1", initial_realm="mortal", source_realm="qi_gathering",
        target_realm="foundation", qi_cost="3", duration_ticks=14_400,
        minimum_practice_skill_level="35", base_chance_percent=35, maximum_skill_bonus=30,
        failure_damage=8,
    ),
)


def cultivation_breakthrough_policies(state: WorldState) -> tuple[CultivationBreakthroughPolicy, ...]:
    """Return fixed, versioned policy terms for the supported world background."""
    if state.metadata.get("pack_id") == "long_life_cultivation":
        return _LONG_LIFE_POLICIES
    return ()


def registered_cultivation_breakthrough_policy(
    state: WorldState, policy_id: str,
) -> CultivationBreakthroughPolicy | None:
    return next((policy for policy in cultivation_breakthrough_policies(state) if policy.policy_id == policy_id), None)


class BreakthroughLotConsumption(_BreakthroughModel):
    lot_id: UUID
    holder_account_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class BreakthroughResourceFlow(_BreakthroughModel):
    holder_account_id: UUID
    sink_account_id: UUID
    resource_id: Literal["cultivation.qi"] = CULTIVATION_QI_RESOURCE_ID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class BreakthroughAccountExpectation(_BreakthroughModel):
    """Compare-and-swap data for each exact ledger posting in the plan."""

    account_id: UUID
    resource_id: Literal["cultivation.qi"] = CULTIVATION_QI_RESOURCE_ID
    account_type: Literal["holder", "consumption_sink"]
    owner_id: UUID | None = None
    expected_balance: ExactDecimal
    resulting_balance: ExactDecimal

    @model_validator(mode="after")
    def account_expectation_is_valid(self) -> BreakthroughAccountExpectation:
        if self.account_type == "holder":
            if self.owner_id is None or self.resulting_balance < 0:
                raise ValueError("a holder expectation requires an owner and a nonnegative resulting balance")
        elif self.owner_id is not None or self.resulting_balance < self.expected_balance:
            raise ValueError("a consumption sink expectation must be unowned and receive resources")
        return self


class CultivationBreakthroughPlan(_BreakthroughModel):
    """Resolved deterministic outcome facts and exact state effect expectations."""

    attempt_id: UUID
    qi_consumption_event_id: UUID
    progression_event_id: UUID | None = None
    failure_damage_event_id: UUID | None = None
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    policy_id: ResourceName
    policy_version: Literal["cultivation.breakthrough.v1"]
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_realm: str = Field(min_length=1, max_length=_REALM_ID_MAX_LENGTH)
    source_realm: str = Field(min_length=1, max_length=_REALM_ID_MAX_LENGTH)
    target_realm: str = Field(min_length=1, max_length=_REALM_ID_MAX_LENGTH)
    started_tick: int = Field(ge=-(2**63), le=_MAX_WORLD_TICK)
    resolved_tick: int = Field(ge=-(2**63), le=_MAX_WORLD_TICK)
    duration_ticks: StrictInt = Field(gt=0, le=_MAX_BREAKTHROUGH_DURATION)
    outcome: Literal["succeeded", "failed"]
    roll_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    roll: StrictInt = Field(ge=1, le=100)
    chance_percent: StrictInt = Field(ge=5, le=95)
    practice_skill_level: ExactDecimal = Field(ge=Decimal("0"), le=Decimal("100"))
    practice_skill_bonus: StrictInt = Field(ge=0, le=100)
    expected_skills_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    expected_progression_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    expected_progression_milestones: list[str] = Field(max_length=256)
    realm_milestone_to_add: str | None = Field(default=None, max_length=160)
    resulting_progression: ProgressionComponent | None = None
    expected_health_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    failure_damage: StrictInt | None = Field(default=None, ge=1, le=_MAX_FAILURE_DAMAGE)
    failure_injury_type: ResourceName | None = None
    qi_resource_id: Literal["cultivation.qi"] = CULTIVATION_QI_RESOURCE_ID
    qi_cost: ExactDecimal = Field(gt=Decimal("0"))
    expected_inventory_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    resulting_inventory: InventoryComponent
    sink_account_id: UUID
    consumed_lots: list[BreakthroughLotConsumption] = Field(min_length=1, max_length=100_000)
    resource_flows: list[BreakthroughResourceFlow] = Field(min_length=1, max_length=100_000)
    account_expectations: list[BreakthroughAccountExpectation] = Field(min_length=2, max_length=100_001)

    @model_validator(mode="after")
    def outcome_and_effect_expectations_are_exact(self) -> CultivationBreakthroughPlan:
        if self.started_tick > _MAX_WORLD_TICK - self.duration_ticks:
            raise ValueError("breakthrough interval exceeds the signed world-clock range")
        if self.resolved_tick != self.started_tick + self.duration_ticks:
            raise ValueError("breakthrough must use its registered exact duration")
        if self.roll != int(self.roll_digest, 16) % 100 + 1:
            raise ValueError("breakthrough roll must match its deterministic digest")
        if (self.outcome == "succeeded") != (self.roll <= self.chance_percent):
            raise ValueError("breakthrough outcome must match the recorded roll and chance")
        if self.resulting_inventory.revision != self.expected_inventory_revision + 1:
            raise ValueError("breakthrough inventory result must advance exactly one revision")

        with localcontext() as context:
            context.prec = 80
            lot_total = sum((item.quantity for item in self.consumed_lots), Decimal("0"))
            flow_totals: dict[UUID, Decimal] = defaultdict(lambda: Decimal("0"))
            for item in self.consumed_lots:
                flow_totals[item.holder_account_id] += item.quantity
        if lot_total != self.qi_cost:
            raise ValueError("breakthrough FIFO lot quantities must exactly equal its qi cost")
        flow_by_account = {flow.holder_account_id: flow for flow in self.resource_flows}
        if len(flow_by_account) != len(self.resource_flows) or {
            account_id: flow.quantity for account_id, flow in flow_by_account.items()
        } != dict(flow_totals):
            raise ValueError("breakthrough resource flows must exactly aggregate the consumed FIFO lots")
        if any(flow.sink_account_id != self.sink_account_id for flow in self.resource_flows):
            raise ValueError("all breakthrough qi flows must use the registered sink account")

        expectations = {item.account_id: item for item in self.account_expectations}
        if len(expectations) != len(self.account_expectations):
            raise ValueError("breakthrough account expectations must not repeat account IDs")
        if self.sink_account_id not in expectations:
            raise ValueError("breakthrough account expectations must include the qi sink")
        if set(expectations) != {*flow_by_account, self.sink_account_id}:
            raise ValueError("breakthrough account expectations must exactly cover its holder flows and qi sink")
        for flow in self.resource_flows:
            holder = expectations.get(flow.holder_account_id)
            if (
                holder is None
                or holder.account_type != "holder"
                or holder.owner_id != self.actor_id
                or holder.resulting_balance != holder.expected_balance - flow.quantity
            ):
                raise ValueError("breakthrough holder expectation must match its exact qi flow")
        sink = expectations[self.sink_account_id]
        if (
            sink.account_type != "consumption_sink"
            or sink.resulting_balance != sink.expected_balance + self.qi_cost
        ):
            raise ValueError("breakthrough sink expectation must receive the exact qi cost")

        marker = realm_milestone(self.target_realm)
        if self.outcome == "succeeded":
            if (
                self.progression_event_id != cultivation_breakthrough_effect_id(
                    self.command_id, self.ruleset_version, "success"
                )
                or self.failure_damage_event_id is not None
                or self.failure_damage is not None
                or self.failure_injury_type is not None
                or self.realm_milestone_to_add != marker
                or self.resulting_progression is None
                or self.resulting_progression.revision != self.expected_progression_revision + 1
                or self.resulting_progression.milestones != [*self.expected_progression_milestones, marker]
            ):
                raise ValueError("breakthrough success must append only its registered realm milestone")
        elif (
            self.progression_event_id is not None
            or self.realm_milestone_to_add is not None
            or self.resulting_progression is not None
            or self.failure_damage_event_id != cultivation_breakthrough_effect_id(
                self.command_id, self.ruleset_version, "failure_damage"
            )
            or self.failure_damage is None
            or self.failure_injury_type != CULTIVATION_FAILURE_INJURY_TYPE
        ):
            raise ValueError("breakthrough failure must carry its registered damage consequence only")
        return self


def realm_milestone(realm_id: str) -> str:
    """Encode one abstract realm as a bounded progression milestone."""

    marker = f"{CULTIVATION_REALM_MILESTONE_PREFIX}{realm_id}"
    if len(marker) > 160:
        raise ValueError("cultivation realm marker exceeds the progression milestone length cap")
    return marker


def cultivation_breakthrough_roll_digest(
    *, world_id: UUID, branch_id: UUID, command_id: UUID, ruleset_version: str,
) -> str:
    """Derive a command-scoped SHA-256 seed matching the existing action rolls."""

    material = ":".join((
        str(world_id),
        str(branch_id),
        str(command_id),
        ruleset_version,
        CULTIVATION_BREAKTHROUGH_ROLL_LABEL,
    )).encode("utf-8")
    return sha256(material).hexdigest()


def cultivation_breakthrough_effect_id(command_id: UUID, ruleset_version: str, effect: str) -> UUID:
    """Derive one stable effect identity from the action command and ruleset."""

    if not isinstance(command_id, UUID) or not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("breakthrough effect identity requires a command UUID and ruleset version")
    if effect not in {"attempt", "qi_consumption", "success", "failure_damage"}:
        raise ValueError("breakthrough effect identity is not registered")
    suffix = "attempt" if effect == "attempt" else effect
    return uuid5(command_id, f"{ruleset_version}:cultivation_breakthrough.{suffix}")


def cultivation_breakthrough_policy_digest(policy: CultivationBreakthroughPolicy) -> str:
    """Hash the complete normalized policy so later event validation can bind it."""

    encoded = json.dumps(
        policy.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(b"world-engine:cultivation-breakthrough-policy:v1\0" + encoded).hexdigest()


def plan_cultivation_breakthrough(
    state: WorldState,
    *,
    actor_id: UUID,
    policy: CultivationBreakthroughPolicy,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
) -> CultivationBreakthroughPlan:
    """Plan exactly one real, timed breakthrough attempt without mutating state.

    The resource debit is identical on success and failure. FIFO lot eligibility
    is checked at the completion tick, so qi expiring at or before completion
    cannot fund the attempt. A repeated call with the same command and inputs
    returns the same roll, IDs, lots, and effects.
    """

    if not isinstance(policy, CultivationBreakthroughPolicy):
        raise CultivationBreakthroughError("policy_invalid", "A registered cultivation breakthrough policy is required.")
    if not isinstance(command_id, UUID) or not isinstance(world_id, UUID) or not isinstance(branch_id, UUID):
        raise CultivationBreakthroughError("command_context_invalid", "Breakthrough planning requires world, branch, and command IDs.")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip() or len(ruleset_version) > 100:
        raise CultivationBreakthroughError("ruleset_invalid", "Breakthrough planning requires a bounded ruleset version.")
    if state.current_tick > _MAX_WORLD_TICK - policy.duration_ticks:
        raise CultivationBreakthroughError("duration_overflow", "The timed attempt exceeds the signed world-clock range.")
    resolved_tick = state.current_tick + policy.duration_ticks

    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise CultivationBreakthroughError("actor_unavailable", "The breakthrough actor must be an active character.")

    capabilities = _component(
        CapabilitiesComponent,
        actor.components.get("capabilities"),
        "capabilities_invalid",
        "The breakthrough actor has no valid capability record.",
    )
    if (
        CULTIVATION_PRACTICE_CAPABILITY not in capabilities.grants
        or CULTIVATION_PRACTICE_CAPABILITY in capabilities.restrictions
    ):
        raise CultivationBreakthroughError(
            "cultivation_capability_missing",
            "The actor must have unrestricted cultivation practice capability.",
        )

    skills = _component(SkillsComponent, actor.components.get("skills"), "skills_invalid", "The actor has no valid skill record.")
    practice = skills.skills.get(CULTIVATION_PRACTICE_SKILL_ID)
    if practice is None:
        raise CultivationBreakthroughError("practice_skill_missing", "The actor has no registered cultivation practice skill.")
    if practice.level < policy.minimum_practice_skill_level:
        raise CultivationBreakthroughError("practice_skill_insufficient", "Cultivation practice skill is below this policy's minimum.")

    progression = _component(
        ProgressionComponent,
        actor.components.get("progression"),
        "progression_invalid",
        "The actor has no valid progression record.",
    )
    target_marker = realm_milestone(policy.target_realm)
    realm_markers = [
        marker[len(CULTIVATION_REALM_MILESTONE_PREFIX):]
        for marker in progression.milestones
        if marker.startswith(CULTIVATION_REALM_MILESTONE_PREFIX)
    ]
    current_realm = realm_markers[-1] if realm_markers else policy.initial_realm
    if current_realm != policy.source_realm:
        raise CultivationBreakthroughError(
            "source_realm_mismatch",
            "The most recent cultivation realm does not match this policy's source realm.",
        )
    if target_marker in progression.milestones:
        raise CultivationBreakthroughError("target_realm_already_reached", "This realm milestone is already recorded.")
    if progression.revision >= _MAX_REVISION:
        raise CultivationBreakthroughError("progression_revision_exhausted", "The progression revision cannot advance further.")
    if len(progression.milestones) >= 256:
        raise CultivationBreakthroughError("progression_milestones_full", "No additional realm milestone fits in the progression record.")

    health = _component(HealthComponent, actor.components.get("health"), "health_invalid", "The actor has no valid health record.")
    if health.current <= 0:
        raise CultivationBreakthroughError("health_depleted", "An actor with depleted health cannot begin a breakthrough.")

    inventory = _component(
        InventoryComponent,
        actor.components.get("inventory"),
        "inventory_invalid",
        "The actor has no valid inventory record.",
    )
    if inventory.revision >= _MAX_REVISION:
        raise CultivationBreakthroughError("inventory_revision_exhausted", "The inventory revision cannot advance further.")
    try:
        allocations = allocate_fifo_lots(
            inventory,
            resource_id=policy.qi_resource_id,
            quantity=policy.qi_cost,
            at_tick=resolved_tick,
        )
    except ValueError as exc:
        raise CultivationBreakthroughError(
            "qi_unavailable_at_completion",
            "Unexpired cultivation qi lots do not cover the exact policy cost at completion.",
        ) from exc

    sink_id, sink = _qi_consumption_sink(state)
    holder_totals = _allocation_totals(allocations)
    holder_accounts: dict[UUID, ResourceAccount] = {}
    for account_id, quantity in holder_totals.items():
        account = state.resources.get(account_id)
        if (
            account is None
            or account.account_type != "holder"
            or account.owner_id != actor_id
            or account.resource_id != policy.qi_resource_id
            or account.balance < quantity
        ):
            raise CultivationBreakthroughError(
                "qi_ledger_mismatch",
                "A FIFO qi lot is not covered by the actor's matching holder account.",
            )
        holder_accounts[account_id] = account

    try:
        resulting_inventory = reduce_fifo_inventory(inventory, allocations)
    except ValueError as exc:
        raise CultivationBreakthroughError("qi_inventory_invalid", "The exact FIFO qi allocation cannot be applied.") from exc

    bonus_steps = int((practice.level - policy.minimum_practice_skill_level).to_integral_value(rounding=ROUND_FLOOR))
    skill_bonus = min(policy.maximum_skill_bonus, bonus_steps * policy.skill_bonus_per_point)
    chance = max(5, min(95, policy.base_chance_percent + skill_bonus))
    digest = cultivation_breakthrough_roll_digest(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
    )
    roll = int(digest, 16) % 100 + 1
    succeeded = roll <= chance

    flows = [
        BreakthroughResourceFlow(
            holder_account_id=account_id,
            sink_account_id=sink_id,
            resource_id=policy.qi_resource_id,
            quantity=quantity,
        )
        for account_id, quantity in sorted(holder_totals.items(), key=lambda item: str(item[0]))
    ]
    expectations: list[BreakthroughAccountExpectation] = [
        BreakthroughAccountExpectation(
            account_id=account_id,
            resource_id=account.resource_id,
            account_type="holder",
            owner_id=actor_id,
            expected_balance=account.balance,
            resulting_balance=account.balance - holder_totals[account_id],
        )
        for account_id, account in sorted(holder_accounts.items(), key=lambda item: str(item[0]))
    ]
    expectations.append(BreakthroughAccountExpectation(
        account_id=sink_id,
        resource_id=sink.resource_id,
        account_type="consumption_sink",
        owner_id=None,
        expected_balance=sink.balance,
        resulting_balance=sink.balance + policy.qi_cost,
    ))

    next_progression = (
        progression.model_copy(update={
            "revision": progression.revision + 1,
            "milestones": [*progression.milestones, target_marker],
        })
        if succeeded else None
    )
    return CultivationBreakthroughPlan(
        attempt_id=cultivation_breakthrough_effect_id(command_id, ruleset_version, "attempt"),
        qi_consumption_event_id=cultivation_breakthrough_effect_id(command_id, ruleset_version, "qi_consumption"),
        progression_event_id=(
            cultivation_breakthrough_effect_id(command_id, ruleset_version, "success") if succeeded else None
        ),
        failure_damage_event_id=(
            cultivation_breakthrough_effect_id(command_id, ruleset_version, "failure_damage") if not succeeded else None
        ),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_digest=cultivation_breakthrough_policy_digest(policy),
        initial_realm=policy.initial_realm,
        source_realm=policy.source_realm,
        target_realm=policy.target_realm,
        started_tick=state.current_tick,
        resolved_tick=resolved_tick,
        duration_ticks=policy.duration_ticks,
        outcome="succeeded" if succeeded else "failed",
        roll_digest=digest,
        roll=roll,
        chance_percent=chance,
        practice_skill_level=practice.level,
        practice_skill_bonus=skill_bonus,
        expected_skills_revision=skills.revision,
        expected_progression_revision=progression.revision,
        expected_progression_milestones=list(progression.milestones),
        realm_milestone_to_add=target_marker if succeeded else None,
        resulting_progression=next_progression,
        expected_health_revision=health.revision,
        failure_damage=None if succeeded else policy.failure_damage,
        failure_injury_type=None if succeeded else CULTIVATION_FAILURE_INJURY_TYPE,
        qi_resource_id=policy.qi_resource_id,
        qi_cost=policy.qi_cost,
        expected_inventory_revision=inventory.revision,
        resulting_inventory=resulting_inventory,
        sink_account_id=sink_id,
        consumed_lots=[
            BreakthroughLotConsumption(
                lot_id=allocation.lot_id,
                holder_account_id=allocation.account_id,
                quantity=allocation.quantity,
            )
            for allocation in allocations
        ],
        resource_flows=flows,
        account_expectations=expectations,
    )


def _component(model: type, value: object, code: str, message: str):
    try:
        return model.model_validate(value)
    except Exception as exc:
        raise CultivationBreakthroughError(code, message) from exc


def _qi_consumption_sink(state: WorldState) -> tuple[UUID, ResourceAccount]:
    candidates = sorted(
        (
            (account_id, account)
            for account_id, account in state.resources.items()
            if account.resource_id == CULTIVATION_QI_RESOURCE_ID and account.account_type == "consumption_sink"
        ),
        key=lambda item: str(item[0]),
    )
    if not candidates:
        raise CultivationBreakthroughError(
            "qi_sink_missing",
            "The world has no registered consumption sink for cultivation qi.",
        )
    return candidates[0]


def _allocation_totals(allocations: list[FifoLotAllocation]) -> dict[UUID, Decimal]:
    totals: dict[UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    with localcontext() as context:
        context.prec = 80
        for allocation in allocations:
            totals[allocation.account_id] += allocation.quantity
    return dict(totals)
