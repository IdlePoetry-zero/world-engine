"""Bind verified v2 promise settlement to a branch-local trust contribution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from .errors import InvalidEvent
from .investigation import entity_visible_to_actor
from .promise_availability import assess_return_promise
from .promise_events import PromiseDeadlineBreachedEvent, PromiseItemReturnedEvent
from .promise_late_return_event import PromiseItemReturnedLateEvent
from .promise_reducers import apply_late_promise_return, apply_promise_event
from .promise_trust import (
    PROMISE_BREACHED_EVENT_V2, PROMISE_LATE_RETURN_EVENT_V1, PROMISE_RETURNED_EVENT_V2,
    PromiseBreachEvidence, PromiseFulfillmentEvidence, PromiseLateReturnEvidence,
    PromiseTrustBondSnapshot, PromiseTrustCas, PromiseTrustCommandContext,
    plan_promise_trust_effect, promise_trust_relation_id,
)
from .promise_trust_events import PromiseDeadlineBreachedWithTrustEvent, PromiseItemReturnedWithTrustEvent
from .promise_trust_state import PromiseTrustProjection, apply_promise_trust_plan
from .relationship_promises import PROMISE_TRUST_POLICY_ID, PromiseReviewReceipt, RelationshipPromise

if TYPE_CHECKING:
    from .models import WorldState


BaseSettlement = PromiseItemReturnedEvent | PromiseDeadlineBreachedEvent
TrustedSettlement = PromiseItemReturnedWithTrustEvent | PromiseDeadlineBreachedWithTrustEvent | PromiseItemReturnedLateEvent
PROMISE_TRUST_POLICY_KEY = "promise_trust_policy_id"


def configured_promise_trust_policy(state: WorldState) -> Literal["core.promise_trust.v1"] | None:
    policy = state.metadata.get(PROMISE_TRUST_POLICY_KEY)
    if policy is None:
        return None
    if policy != PROMISE_TRUST_POLICY_ID:
        raise ValueError("World promise trust policy is unavailable.")
    return PROMISE_TRUST_POLICY_ID


def build_trusted_promise_settlement(
    state: WorldState, event: BaseSettlement, *, source_breach_event_id: UUID | None = None,
) -> BaseSettlement | TrustedSettlement:
    """Wrap a registered settlement only if its promise pins the trust policy."""
    promise = next((item for item in state.relationship_promises if item.id == event.promise_id), None)
    if promise is None:
        raise InvalidEvent("Promise settlement references a missing promise.")
    if promise.trust_policy_id is None:
        if source_breach_event_id is not None:
            raise InvalidEvent("Late return requires a promise with a pinned trust policy.")
        return event
    schedule = next((item for item in state.scheduled_events if item.id == event.scheduled_event_id), None)
    if schedule is None:
        raise InvalidEvent("Promise settlement references a missing schedule.")

    projection = _read_projection(state, event)
    relation_id = promise_trust_relation_id(event.world_id, promise.promisee_id, promise.promisor_id)
    bond = projection.bonds.get(relation_id)
    snapshot = None if bond is None else PromiseTrustBondSnapshot(
        relation_id=bond.id, from_id=bond.from_id, to_id=bond.to_id, revision=bond.revision,
        trust=bond.trust, budget_policy_id=bond.policy_id,
        success_budget_spent=bond.success_budget_spent, evidence_refs=bond.evidence_refs,
    )
    cas = PromiseTrustCas(relation_id=relation_id) if snapshot is None else PromiseTrustCas(
        relation_id=relation_id, expected_relation_revision=snapshot.revision,
        expected_previous_trust=snapshot.trust, expected_budget_policy_id=snapshot.budget_policy_id,
        expected_success_budget_spent=snapshot.success_budget_spent,
    )
    command = PromiseTrustCommandContext(
        world_id=event.world_id, branch_id=event.branch_id, command_id=event.command_id,
        actor_id=event.actor_id, ruleset_version=event.ruleset_version, current_tick=state.current_tick,
    )
    evidence_fields = command.model_dump(exclude={"current_tick"}) | {
        "event_id": event.event_id, "promise_id": event.promise_id,
        "expected_promise_revision": event.expected_promise_revision,
        "promisor_id": promise.promisor_id, "promisee_id": promise.promisee_id,
        "scheduled_event_id": event.scheduled_event_id, "expected_schedule_revision": event.expected_schedule_revision,
        "policy_id": promise.trust_policy_id, "trust_cas": cas,
    }
    if isinstance(event, PromiseItemReturnedEvent):
        transfer_fields = evidence_fields | {
            "effective_tick": event.transfer_tick, "source_owner_id": event.source_owner_id,
            "recipient_owner_id": event.recipient_owner_id, "resource_id": event.resource_id, "quantity": event.quantity,
        }
        if source_breach_event_id is None:
            evidence = PromiseFulfillmentEvidence(
                **transfer_fields, outcome="on_time_fulfilled", event_type=PROMISE_RETURNED_EVENT_V2,
            )
        else:
            evidence = PromiseLateReturnEvidence(
                **transfer_fields, outcome="late_return", event_type=PROMISE_LATE_RETURN_EVENT_V1,
                source_breach_event_id=source_breach_event_id,
            )
    else:
        if source_breach_event_id is not None:
            raise InvalidEvent("Only a physical return may repair a previous breach.")
        evidence = PromiseBreachEvidence(
            **evidence_fields, outcome="breached", event_type=PROMISE_BREACHED_EVENT_V2,
            effective_tick=event.resolved_tick,
            review_receipt=_applicable_breach_review(state, promise),
        )
    trust = plan_promise_trust_effect(
        promise, evidence, snapshot, schedule, command, promise_policy_id=promise.trust_policy_id,
        prior_receipt=projection.receipts.get(promise.id),
    )
    if trust is None:
        raise InvalidEvent("A configured promise settlement requires its planned trust effect.")
    if isinstance(event, PromiseItemReturnedEvent):
        if source_breach_event_id is not None:
            return PromiseItemReturnedLateEvent(
                event_id=event.event_id, settlement=event, trust=trust, source_breach_event_id=source_breach_event_id,
            )
        return PromiseItemReturnedWithTrustEvent(event_id=event.event_id, settlement=event, trust=trust)
    return PromiseDeadlineBreachedWithTrustEvent(event_id=event.event_id, settlement=event, trust=trust)


def _applicable_breach_review(state: WorldState, promise: RelationshipPromise) -> PromiseReviewReceipt | None:
    receipt = promise.review_receipt
    if receipt is None or receipt.choice == "refuse":
        return receipt
    # A once-observed obstacle does not justify mitigation after it clears.
    # The cutoff check does not extend the inclusive deadline or attempt a return.
    actor = state.entities[promise.promisor_id]
    visible = {promise.promisee_id} if entity_visible_to_actor(state, actor.id, promise.promisee_id) else set()
    delivery, stock = assess_return_promise(state, actor, promise, visible_counterpart_ids=visible)
    if actor.status == "active" and delivery == "recipient_available_here" and stock:
        return None
    return receipt


def apply_trusted_promise_settlement(state: WorldState, event: TrustedSettlement) -> WorldState:
    """Verify the recorded plan and apply trust and the original settlement atomically."""
    source_breach = event.source_breach_event_id if isinstance(event, PromiseItemReturnedLateEvent) else None
    expected = build_trusted_promise_settlement(state, event.settlement, source_breach_event_id=source_breach)
    if type(expected) is not type(event) or expected.model_dump(mode="json") != event.model_dump(mode="json"):
        raise InvalidEvent("Recorded trust effect does not match the canonical promise settlement.")
    projection = apply_promise_trust_plan(
        _read_projection(state, event.settlement), event.trust,
        expected_world_id=event.world_id, expected_branch_id=event.branch_id,
    )
    updated = (apply_promise_event(state, event.settlement) if source_breach is None
               else apply_late_promise_return(state, event.settlement, source_breach))
    return type(state).model_validate(updated.model_dump(mode="python") | {"promise_trust": projection.to_payload()})


def _read_projection(state: WorldState, event: BaseSettlement) -> PromiseTrustProjection:
    if state.promise_trust is None:
        return PromiseTrustProjection.empty(event.world_id, event.branch_id)
    projection = PromiseTrustProjection.from_payload(state.promise_trust, branch_id=event.branch_id)
    if projection.world_id != event.world_id:
        raise InvalidEvent("Promise settlement does not belong to the projected world.")
    return projection


def validate_promise_trust_state(state: WorldState) -> None:
    policy = configured_promise_trust_policy(state)
    if any(item.trust_policy_id is not None and item.trust_policy_id != policy for item in state.relationship_promises):
        raise ValueError("A promise's pinned trust policy must be installed in this world.")
    if state.promise_trust is None:
        return
    try:
        projection = PromiseTrustProjection.from_payload(state.promise_trust)
    except InvalidEvent as exc:
        raise ValueError(str(exc)) from exc
    promises = {item.id: item for item in state.relationship_promises}
    for promise_id, receipt in projection.receipts.items():
        promise = promises.get(promise_id)
        status = "fulfilled" if receipt.outcome == "on_time_fulfilled" else "breached"
        if (promise is None or promise.status != status or promise.trust_policy_id != receipt.policy_id
                or promise.outcome_event_id != receipt.outcome_event_id):
            raise ValueError("Promise trust receipt must match the stored promise and outcome.")
        bond = projection.bonds.get(promise_trust_relation_id(
            projection.world_id, promise.promisee_id, promise.promisor_id,
        ))
        if bond is None or receipt.outcome_event_id not in bond.evidence_refs:
            raise ValueError("Promise trust receipt must be attributed to its original directed bond.")
