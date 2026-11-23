"""Pure, versioned trust-effect planning for return-promise outcomes.

The planner does not mutate promises or relationships. A registered host event
builder captures its plan as audit/CAS fields, and the reducer revalidates the
plan against the same promise, scheduled event, and directed relation snapshot.
Legacy promises with no assigned policy return no trust effect.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .components import ExactDecimal, ResourceName, ScheduledEventRecord, SignedRelationValue, Tick
from .errors import InvalidEvent
from .relationship_promises import (
    PROMISE_DEADLINE_HANDLER,
    PROMISE_DEADLINE_HANDLER_VERSION,
    PROMISE_TRUST_DEADLINE_HANDLER_VERSION,
    PROMISE_TRUST_POLICY_ID,
    PromiseReviewReceipt, RelationshipPromise,
)


PROMISE_RECORDED_EVENT_V1 = "core.relationship_promise_recorded.v1"
PROMISE_RETURNED_EVENT_V1 = "core.promise_item_returned.v1"
PROMISE_RETURNED_EVENT_V2 = "core.promise_item_returned.v2"
PROMISE_BREACHED_EVENT_V1 = "core.promise_breached.v1"
PROMISE_BREACHED_EVENT_V2 = "core.promise_breached.v2"
PROMISE_RELEASED_EVENT_V1 = "core.promise_released.v1"
PROMISE_LATE_RETURN_EVENT_V1 = "core.promise_item_returned_late.v1"
_MAX_SUCCESS_BUDGET = Decimal("100")
_MAX_POLICY_REWARD = Decimal("10")
_MAX_POLICY_PENALTY = Decimal("20")
_MAX_POLICY_REPAIR = Decimal("10")


class PromiseTrustModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class PromiseTrustPolicy(PromiseTrustModel):
    """Authored, versioned policy for new promises; legacy records opt out."""

    policy_id: ResourceName = PROMISE_TRUST_POLICY_ID
    on_time_reward: ExactDecimal = Field(default=Decimal("3"), gt=Decimal("0"), le=_MAX_POLICY_REWARD)
    breach_penalty: SignedRelationValue = Field(default=Decimal("-8"), ge=_MAX_POLICY_PENALTY.copy_negate(), lt=Decimal("0"))
    late_return_repair: ExactDecimal = Field(default=Decimal("2"), gt=Decimal("0"), le=_MAX_POLICY_REPAIR)
    success_budget_cap: ExactDecimal = Field(default=Decimal("6"), ge=Decimal("0"), le=_MAX_SUCCESS_BUDGET)

    @model_validator(mode="after")
    def repair_cannot_exceed_policy_loss(self) -> PromiseTrustPolicy:
        if self.late_return_repair >= self.breach_penalty.copy_abs():
            raise ValueError("late return repair must be smaller than the registered breach penalty")
        return self


DEFAULT_PROMISE_TRUST_POLICY = PromiseTrustPolicy()


class PromiseTrustCommandContext(PromiseTrustModel):
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    current_tick: Tick


class PromiseTrustCas(PromiseTrustModel):
    """Trusted relation snapshot CAS copied into a versioned outcome event."""

    relation_id: UUID
    expected_relation_revision: StrictInt | None = Field(default=None, ge=1)
    expected_previous_trust: SignedRelationValue | None = None
    expected_budget_policy_id: ResourceName | None = None
    expected_success_budget_spent: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"), le=_MAX_SUCCESS_BUDGET)


class PromiseTrustOutcomeBase(PromiseTrustModel):
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    event_id: UUID
    event_type: str = Field(min_length=1, max_length=160)
    promise_id: UUID
    expected_promise_revision: StrictInt = Field(ge=1)
    promisor_id: UUID
    promisee_id: UUID
    effective_tick: Tick
    scheduled_event_id: UUID
    expected_schedule_revision: StrictInt = Field(ge=1)
    policy_id: ResourceName | None = None
    trust_cas: PromiseTrustCas | None = None


class PromiseFulfillmentEvidence(PromiseTrustOutcomeBase):
    outcome: Literal["on_time_fulfilled"]
    event_type: Literal[PROMISE_RETURNED_EVENT_V1, PROMISE_RETURNED_EVENT_V2]
    source_owner_id: UUID
    recipient_owner_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class PromiseBreachEvidence(PromiseTrustOutcomeBase):
    outcome: Literal["breached"]
    event_type: Literal[PROMISE_BREACHED_EVENT_V1, PROMISE_BREACHED_EVENT_V2]
    review_receipt: PromiseReviewReceipt | None = None


class PromiseLateReturnEvidence(PromiseTrustOutcomeBase):
    outcome: Literal["late_return"]
    event_type: Literal[PROMISE_LATE_RETURN_EVENT_V1]
    source_owner_id: UUID
    recipient_owner_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    source_breach_event_id: UUID


class PromiseReleaseEvidence(PromiseTrustOutcomeBase):
    outcome: Literal["released"]
    event_type: Literal[PROMISE_RELEASED_EVENT_V1]


PromiseTrustOutcomeEvidence = Annotated[
    PromiseFulfillmentEvidence
    | PromiseBreachEvidence
    | PromiseLateReturnEvidence
    | PromiseReleaseEvidence,
    Field(discriminator="outcome"),
]

PROMISE_TRUST_OUTCOME_ADAPTER = TypeAdapter(PromiseTrustOutcomeEvidence)


class PromiseTrustBondSnapshot(PromiseTrustModel):
    """One pre-existing directed bond, or an absent bond before first effect."""

    relation_id: UUID | None = None
    from_id: UUID
    to_id: UUID
    revision: StrictInt | None = Field(default=None, ge=1)
    trust: SignedRelationValue | None = None
    budget_policy_id: ResourceName | None = None
    success_budget_spent: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"), le=_MAX_SUCCESS_BUDGET)
    evidence_refs: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def bond_snapshot_is_coherent(self) -> PromiseTrustBondSnapshot:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("trust bond evidence references must be unique")
        if self.relation_id is None:
            if (
                self.revision is not None
                or self.trust is not None
                or self.budget_policy_id is not None
                or self.success_budget_spent != 0
                or self.evidence_refs
            ):
                raise ValueError("an absent trust bond cannot carry revision, trust, budget, or evidence")
        elif self.revision is None:
            raise ValueError("an existing trust bond requires a revision")
        if self.budget_policy_id is None and self.success_budget_spent != 0:
            raise ValueError("a spent success budget requires its policy ID")
        return self


class PromiseTrustReceipt(PromiseTrustModel):
    """Immutable causal result persisted with the corresponding promise."""

    policy_id: ResourceName
    promise_id: UUID
    outcome_event_id: UUID
    outcome: Literal["on_time_fulfilled", "breached"]
    applied_delta: SignedRelationValue
    assessed_choice: Literal["blocked", "refuse"] | None = Field(default=None, exclude_if=lambda value: value is None)
    late_return_event_id: UUID | None = None
    late_return_delta: SignedRelationValue | None = None

    @model_validator(mode="after")
    def receipt_has_one_late_repair(self) -> PromiseTrustReceipt:
        if self.assessed_choice is not None and self.outcome != "breached":
            raise ValueError("Only a breach may have a prior nonperformance assessment.")
        if self.outcome != "breached" and (self.late_return_event_id is not None or self.late_return_delta is not None):
            raise ValueError("only a breached promise can retain a late-return repair receipt")
        if (self.late_return_event_id is None) != (self.late_return_delta is None):
            raise ValueError("late-return event and trust delta must be recorded together")
        if self.late_return_delta is not None and self.late_return_delta < 0:
            raise ValueError("late-return repair delta cannot reduce trust")
        return self


class PromiseTrustPlan(PromiseTrustModel):
    """A pure effect plan with exact before-values and bounded after-values."""

    policy_id: ResourceName
    promise_id: UUID
    outcome: Literal["on_time_fulfilled", "breached", "late_return"]
    outcome_event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str
    effective_tick: Tick
    relation_id: UUID
    from_id: UUID
    to_id: UUID
    expected_relation_revision: StrictInt | None = Field(default=None, ge=1)
    expected_previous_trust: SignedRelationValue | None = None
    expected_budget_policy_id: ResourceName | None = None
    expected_success_budget_spent: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"), le=_MAX_SUCCESS_BUDGET)
    applied_delta: SignedRelationValue
    resulting_trust: SignedRelationValue
    next_budget_policy_id: ResourceName
    next_success_budget_spent: ExactDecimal = Field(ge=Decimal("0"), le=_MAX_SUCCESS_BUDGET)
    evidence_refs_to_append: tuple[UUID, ...]
    receipt: PromiseTrustReceipt


def parse_promise_trust_outcome(
    value: PromiseTrustOutcomeEvidence | dict[str, object],
) -> PromiseTrustOutcomeEvidence:
    """Parse the closed set of host-authored promise outcome evidence."""

    raw_value = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
    return PROMISE_TRUST_OUTCOME_ADAPTER.validate_python(raw_value)


def plan_promise_trust_effect(
    promise: RelationshipPromise,
    outcome: PromiseTrustOutcomeEvidence | dict[str, object],
    bond: PromiseTrustBondSnapshot | None,
    schedule: ScheduledEventRecord,
    context: PromiseTrustCommandContext,
    *,
    promise_policy_id: str | None,
    policy: PromiseTrustPolicy = DEFAULT_PROMISE_TRUST_POLICY,
    prior_receipt: PromiseTrustReceipt | None = None,
) -> PromiseTrustPlan | None:
    """Plan one verified outcome effect without mutating promise or bond.

    ``promise_policy_id`` is the host adapter for the future per-promise policy
    marker. ``None`` denotes a legacy v1 promise: its outcome is validated but
    produces no trust plan. An on-time return, breach, and late return must be
    passed only after the registered event/reducer has established the actual
    return ledger/FIFO proof or exact due schedule.
    """

    promise = type(promise).model_validate(promise.model_dump(mode="python"))
    if bond is not None:
        bond = PromiseTrustBondSnapshot.model_validate(bond.model_dump(mode="python"))
    schedule = ScheduledEventRecord.model_validate(schedule.model_dump(mode="python"))
    context = PromiseTrustCommandContext.model_validate(context.model_dump(mode="python"))
    policy = PromiseTrustPolicy.model_validate(policy.model_dump(mode="python"))
    if prior_receipt is not None:
        prior_receipt = PromiseTrustReceipt.model_validate(prior_receipt.model_dump(mode="python"))
    evidence = parse_promise_trust_outcome(outcome)
    _validate_context(evidence, context)
    _validate_promise_identity(promise, evidence)
    _validate_schedule(promise, evidence, schedule, promise_policy_id)
    _validate_outcome_terms(promise, evidence)

    # Until RelationshipPromise owns the marker, integrations pass the value
    # extracted from their versioned adapter. When a typed promise adapter does
    # expose it, require the independently persisted marker to agree as well.
    stored_policy_id = getattr(promise, "trust_policy_id", promise_policy_id)
    if stored_policy_id != promise_policy_id:
        raise InvalidEvent("Promise trust policy does not match its authored promise marker.")

    if promise_policy_id != evidence.policy_id:
        raise InvalidEvent("Promise outcome trust policy does not match its authored promise policy.")
    expected_event_type = _expected_event_type(evidence.outcome, promise_policy_id)
    if evidence.event_type != expected_event_type:
        raise InvalidEvent("Promise outcome event version does not match the assigned trust policy.")
    if promise_policy_id is None:
        if evidence.trust_cas is not None:
            raise InvalidEvent("Legacy promise outcomes cannot carry a trust-effect CAS.")
        if prior_receipt is not None:
            raise InvalidEvent("Legacy promise outcomes cannot carry a trust-effect receipt.")
        return None
    if promise_policy_id != policy.policy_id:
        raise InvalidEvent("Promise trust policy ID is unavailable to this ruleset.")

    if evidence.outcome == "released":
        if evidence.trust_cas is not None or prior_receipt is not None:
            raise InvalidEvent("Releasing a promise does not change trust or carry a trust receipt.")
        return None

    cas = evidence.trust_cas
    if cas is None:
        raise InvalidEvent("Versioned promise trust outcomes require a relation compare-and-swap.")

    directed_from = promise.promisee_id
    directed_to = promise.promisor_id
    snapshot = bond or PromiseTrustBondSnapshot(from_id=directed_from, to_id=directed_to)
    _validate_bond(snapshot, promise, policy)
    relation_id = snapshot.relation_id or promise_trust_relation_id(context.world_id, directed_from, directed_to)
    _validate_trust_cas(cas, snapshot, relation_id)

    if evidence.event_id in snapshot.evidence_refs:
        raise InvalidEvent("Promise trust outcome event has already affected this directed bond.")

    if evidence.outcome == "on_time_fulfilled":
        if prior_receipt is not None:
            raise InvalidEvent("An active promise cannot already have a trust outcome receipt.")
        budget_remaining = policy.success_budget_cap - snapshot.success_budget_spent
        budget_award = min(policy.on_time_reward, budget_remaining)
        trust_before = snapshot.trust if snapshot.trust is not None else Decimal("0")
        resulting_trust = _clamp_relation(trust_before + budget_award)
        applied_delta = resulting_trust - trust_before
        next_budget = snapshot.success_budget_spent + budget_award
        receipt = PromiseTrustReceipt(
            policy_id=policy.policy_id,
            promise_id=promise.id,
            outcome_event_id=evidence.event_id,
            outcome="on_time_fulfilled",
            applied_delta=applied_delta,
        )
        references = (evidence.event_id,)
    elif evidence.outcome == "breached":
        if prior_receipt is not None:
            raise InvalidEvent("An active promise cannot already have a trust outcome receipt.")
        trust_before = snapshot.trust if snapshot.trust is not None else Decimal("0")
        choice = evidence.review_receipt.choice if evidence.review_receipt is not None else None
        penalty = max(policy.breach_penalty, Decimal("-2")) if choice == "blocked" else policy.breach_penalty
        resulting_trust = _clamp_relation(trust_before + penalty)
        applied_delta = resulting_trust - trust_before
        next_budget = snapshot.success_budget_spent
        receipt = PromiseTrustReceipt(
            policy_id=policy.policy_id,
            promise_id=promise.id,
            outcome_event_id=evidence.event_id,
            outcome="breached",
            applied_delta=applied_delta,
            assessed_choice=choice,
        )
        references = (evidence.event_id,)
    else:
        if prior_receipt is None:
            raise InvalidEvent("A late return requires the trust receipt from the original breach.")
        prior_receipt = PromiseTrustReceipt.model_validate(prior_receipt.model_dump(mode="python"))
        if (
            prior_receipt.promise_id != promise.id
            or prior_receipt.policy_id != policy.policy_id
            or prior_receipt.outcome != "breached"
            or prior_receipt.outcome_event_id != evidence.source_breach_event_id
        ):
            raise InvalidEvent("Late return receipt does not reference this promise's original breach.")
        if evidence.source_breach_event_id not in snapshot.evidence_refs:
            raise InvalidEvent("Late return trust bond has no committed reference to the original breach.")
        if prior_receipt.late_return_event_id is not None:
            raise InvalidEvent("A breached promise can receive at most one trust repair from a late return.")
        trust_before = snapshot.trust if snapshot.trust is not None else Decimal("0")
        prior_loss = max(Decimal("0"), prior_receipt.applied_delta.copy_negate())
        repair_ceiling = min(policy.late_return_repair, prior_loss)
        resulting_trust = _clamp_relation(trust_before + repair_ceiling)
        applied_delta = resulting_trust - trust_before
        next_budget = snapshot.success_budget_spent
        receipt = PromiseTrustReceipt.model_validate({
            **prior_receipt.model_dump(mode="python"),
            "late_return_event_id": evidence.event_id,
            "late_return_delta": applied_delta,
        })
        references = (evidence.source_breach_event_id, evidence.event_id)

    with localcontext() as decimal_context:
        decimal_context.prec = 50
        next_budget = +next_budget
    if next_budget > policy.success_budget_cap:
        raise InvalidEvent("Promise trust success budget exceeds its authored cap.")

    return PromiseTrustPlan(
        policy_id=policy.policy_id,
        promise_id=promise.id,
        outcome=evidence.outcome,
        outcome_event_id=evidence.event_id,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        actor_id=context.actor_id,
        ruleset_version=context.ruleset_version,
        effective_tick=evidence.effective_tick,
        relation_id=relation_id,
        from_id=directed_from,
        to_id=directed_to,
        expected_relation_revision=snapshot.revision,
        expected_previous_trust=snapshot.trust,
        expected_budget_policy_id=snapshot.budget_policy_id,
        expected_success_budget_spent=snapshot.success_budget_spent,
        applied_delta=applied_delta,
        resulting_trust=resulting_trust,
        next_budget_policy_id=policy.policy_id,
        next_success_budget_spent=next_budget,
        evidence_refs_to_append=references,
        receipt=receipt,
    )


def promise_trust_relation_id(world_id: UUID, promisee_id: UUID, promisor_id: UUID) -> UUID:
    """Derive the stable directed bond ID used when no relation exists yet."""

    if promisee_id == promisor_id:
        raise InvalidEvent("Promise trust requires distinct participants.")
    return uuid5(world_id, f"relation:{promisee_id}:{promisor_id}")


def _validate_context(evidence: PromiseTrustOutcomeEvidence, context: PromiseTrustCommandContext) -> None:
    if (
        evidence.world_id != context.world_id
        or evidence.branch_id != context.branch_id
        or evidence.command_id != context.command_id
        or evidence.actor_id != context.actor_id
        or evidence.ruleset_version != context.ruleset_version
        or evidence.effective_tick != context.current_tick
    ):
        raise InvalidEvent("Promise trust outcome does not match its trusted command and current tick.")


def _validate_promise_identity(promise: RelationshipPromise, evidence: PromiseTrustOutcomeEvidence) -> None:
    if (
        evidence.promise_id != promise.id
        or evidence.promisor_id != promise.promisor_id
        or evidence.promisee_id != promise.promisee_id
        or evidence.expected_promise_revision != promise.revision
    ):
        raise InvalidEvent("Promise trust outcome does not match the exact promise parties or revision.")
    if evidence.outcome == "late_return":
        if (
            promise.status != "breached"
            or promise.outcome_event_id != evidence.source_breach_event_id
            or promise.resolved_tick != promise.deadline_tick + 1
            or evidence.effective_tick <= promise.deadline_tick
        ):
            raise InvalidEvent("Late return must reference this promise's committed breach and occur after its deadline.")
    elif promise.status != "active":
        raise InvalidEvent("Only an active promise can receive its first outcome effect.")


def _validate_outcome_terms(promise: RelationshipPromise, evidence: PromiseTrustOutcomeEvidence) -> None:
    if evidence.outcome in {"on_time_fulfilled", "late_return"}:
        if (
            evidence.actor_id != promise.promisor_id
            or evidence.source_owner_id != promise.promisor_id
            or evidence.recipient_owner_id != promise.promisee_id
            or evidence.resource_id != promise.terms.resource_id
            or evidence.quantity != promise.terms.quantity
            or evidence.scheduled_event_id != promise.deadline_schedule_id
        ):
            raise InvalidEvent("Promise return outcome does not match the exact parties, resource, quantity, or schedule.")
        if evidence.outcome == "on_time_fulfilled" and not promise.created_tick <= evidence.effective_tick <= promise.deadline_tick:
            raise InvalidEvent("On-time fulfillment must occur on or before the inclusive deadline.")
    elif evidence.outcome == "released":
        if evidence.actor_id != promise.promisee_id or evidence.scheduled_event_id != promise.deadline_schedule_id:
            raise InvalidEvent("Only the promisee may release this exact promise and deadline schedule.")
        if not promise.created_tick <= evidence.effective_tick <= promise.deadline_tick:
            raise InvalidEvent("Promise release must occur on or before the inclusive deadline.")
    elif evidence.outcome == "breached":
        if evidence.review_receipt is not None and evidence.review_receipt != promise.review_receipt:
            raise InvalidEvent("A breach assessment must use the latest committed review on this promise.")
        if evidence.scheduled_event_id != promise.deadline_schedule_id or evidence.effective_tick != promise.deadline_tick + 1:
            raise InvalidEvent("Promise breach must reference the exact deadline schedule at deadline plus one tick.")


def _validate_schedule(
    promise: RelationshipPromise,
    evidence: PromiseTrustOutcomeEvidence,
    schedule: ScheduledEventRecord,
    promise_policy_id: str | None,
) -> None:
    expected_status = "completed" if evidence.outcome == "late_return" else "queued"
    expected_runs = 1 if evidence.outcome == "late_return" else 0
    expected_version = PROMISE_TRUST_DEADLINE_HANDLER_VERSION if promise_policy_id is not None else PROMISE_DEADLINE_HANDLER_VERSION
    if (
        schedule.id != promise.deadline_schedule_id
        or schedule.id != evidence.scheduled_event_id
        or schedule.revision != evidence.expected_schedule_revision
        or schedule.due_tick != promise.deadline_tick + 1
        or schedule.phase != "maintenance"
        or schedule.handler_id != PROMISE_DEADLINE_HANDLER
        or schedule.handler_version != expected_version
        or schedule.status != expected_status
        or schedule.repeat_ticks is not None
        or schedule.max_runs != 1
        or schedule.completed_runs != expected_runs
        or schedule.dependencies
        or schedule.priority != 0
        or schedule.payload != {"promise_id": str(promise.id)}
    ):
        raise InvalidEvent("Promise trust outcome schedule compare-and-swap failed.")


def _validate_bond(
    snapshot: PromiseTrustBondSnapshot,
    promise: RelationshipPromise,
    policy: PromiseTrustPolicy,
) -> None:
    if snapshot.from_id != promise.promisee_id or snapshot.to_id != promise.promisor_id:
        raise InvalidEvent("Promise trust bond must use the directed promisee-to-promisor edge.")
    if snapshot.budget_policy_id not in {None, policy.policy_id}:
        raise InvalidEvent("Pre-existing promise success budget belongs to a different policy.")
    if snapshot.success_budget_spent > policy.success_budget_cap:
        raise InvalidEvent("Pre-existing promise success budget exceeds this policy's cap.")


def _validate_trust_cas(
    cas: PromiseTrustCas,
    snapshot: PromiseTrustBondSnapshot,
    relation_id: UUID,
) -> None:
    if (
        cas.relation_id != relation_id
        or cas.expected_relation_revision != snapshot.revision
        or cas.expected_previous_trust != snapshot.trust
        or cas.expected_budget_policy_id != snapshot.budget_policy_id
        or cas.expected_success_budget_spent != snapshot.success_budget_spent
    ):
        raise InvalidEvent("Promise trust relation compare-and-swap does not match the current directed bond.")


def _expected_event_type(outcome: str, policy_id: str | None) -> str:
    if outcome == "released":
        return PROMISE_RELEASED_EVENT_V1
    if outcome == "late_return":
        return PROMISE_LATE_RETURN_EVENT_V1
    if policy_id is None:
        return PROMISE_RETURNED_EVENT_V1 if outcome == "on_time_fulfilled" else PROMISE_BREACHED_EVENT_V1
    if outcome == "on_time_fulfilled":
        return PROMISE_RETURNED_EVENT_V2
    if outcome == "breached":
        return PROMISE_BREACHED_EVENT_V2
    raise InvalidEvent("This promise outcome has no trust-effect event version.")


def _clamp_relation(value: Decimal) -> Decimal:
    return min(Decimal("100"), max(Decimal("-100"), value))
