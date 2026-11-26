from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.relationship_promises import (
    PROMISE_DEADLINE_HANDLER,
    PROMISE_DEADLINE_HANDLER_VERSION,
    RelationshipPromise,
    ReturnItemTerms,
    create_return_promise,
)
from world_engine.domain.promise_trust import (
    DEFAULT_PROMISE_TRUST_POLICY,
    PROMISE_BREACHED_EVENT_V1,
    PROMISE_BREACHED_EVENT_V2,
    PROMISE_LATE_RETURN_EVENT_V1,
    PROMISE_RELEASED_EVENT_V1,
    PROMISE_RETURNED_EVENT_V1,
    PROMISE_RETURNED_EVENT_V2,
    PROMISE_TRUST_DEADLINE_HANDLER_VERSION,
    PROMISE_TRUST_POLICY_ID,
    PromiseBreachEvidence,
    PromiseFulfillmentEvidence,
    PromiseLateReturnEvidence,
    PromiseReleaseEvidence,
    PromiseTrustBondSnapshot,
    PromiseTrustCas,
    PromiseTrustCommandContext,
    PromiseTrustPolicy,
    PromiseTrustReceipt,
    plan_promise_trust_effect,
    promise_trust_relation_id,
)


WORLD_ID = UUID(int=1000)
BRANCH_ID = UUID(int=1001)
RULESET = "rules.trust-test.v2"
PROMISOR_ID = UUID(int=11)
PROMISEE_ID = UUID(int=12)
SCHEDULE_ID = UUID(int=30)


def uid(value: int) -> UUID:
    return UUID(int=value)


class TrustEnabledPromise(RelationshipPromise):
    """Adapter for the next authored version's per-promise policy marker."""

    trust_policy_id: str | None = None


def make_promise(
    *,
    promise_id: UUID = uid(40),
    trust_policy_id: str | None = PROMISE_TRUST_POLICY_ID,
    status: str = "active",
    revision: int = 1,
    resolved_tick: int | None = None,
    outcome_event_id: UUID | None = None,
    deadline_tick: int = 200,
) -> TrustEnabledPromise:
    promise = create_return_promise(
        promise_id=promise_id,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        private_text="我会把两份药材还给你。",
        terms=ReturnItemTerms(
            resource_id="cultivation.herb",
            quantity="2.5",
            promisee_account_id=uid(22),
        ),
        created_tick=100,
        deadline_tick=deadline_tick,
    )
    return TrustEnabledPromise.model_validate({
        **promise.model_dump(mode="python"),
        "status": status,
        "revision": revision,
        "resolved_tick": resolved_tick,
        "outcome_event_id": outcome_event_id,
        "trust_policy_id": trust_policy_id,
    })


def command_context(*, actor_id: UUID = PROMISOR_ID, command_id: UUID = uid(61), tick: int = 150):
    return PromiseTrustCommandContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=RULESET,
        current_tick=tick,
    )


def deadline_schedule(
    promise: RelationshipPromise,
    *,
    policy_enabled: bool = True,
    status: str = "queued",
    revision: int = 1,
) -> ScheduledEventRecord:
    return ScheduledEventRecord(
        id=promise.deadline_schedule_id,
        due_tick=promise.deadline_tick + 1,
        phase="maintenance",
        handler_id=PROMISE_DEADLINE_HANDLER,
        handler_version=(PROMISE_TRUST_DEADLINE_HANDLER_VERSION if policy_enabled else PROMISE_DEADLINE_HANDLER_VERSION),
        status=status,
        max_runs=1,
        completed_runs=1 if status == "completed" else 0,
        revision=revision,
        payload={"promise_id": str(promise.id)},
    )


def bond_snapshot(
    promise: RelationshipPromise,
    *,
    trust: str | None = "10",
    revision: int | None = 3,
    budget_policy_id: str | None = None,
    success_budget_spent: str = "0",
    evidence_refs: tuple[UUID, ...] = (),
) -> PromiseTrustBondSnapshot:
    return PromiseTrustBondSnapshot(
        relation_id=uid(70) if revision is not None else None,
        from_id=promise.promisee_id,
        to_id=promise.promisor_id,
        revision=revision,
        trust=trust,
        budget_policy_id=budget_policy_id,
        success_budget_spent=success_budget_spent,
        evidence_refs=evidence_refs,
    )


def cas_for(promise: RelationshipPromise, bond: PromiseTrustBondSnapshot | None) -> PromiseTrustCas:
    return PromiseTrustCas(
        relation_id=(
            bond.relation_id if bond is not None and bond.relation_id is not None
            else promise_trust_relation_id(WORLD_ID, promise.promisee_id, promise.promisor_id)
        ),
        expected_relation_revision=bond.revision if bond is not None else None,
        expected_previous_trust=bond.trust if bond is not None else None,
        expected_budget_policy_id=bond.budget_policy_id if bond is not None else None,
        expected_success_budget_spent=bond.success_budget_spent if bond is not None else "0",
    )


def fulfillment_evidence(
    promise: RelationshipPromise,
    bond: PromiseTrustBondSnapshot | None,
    *,
    policy_id: str | None = PROMISE_TRUST_POLICY_ID,
    event_id: UUID = uid(80),
    command_id: UUID = uid(61),
    tick: int = 150,
    revision: int | None = None,
) -> PromiseFulfillmentEvidence:
    return PromiseFulfillmentEvidence(
        outcome="on_time_fulfilled",
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=PROMISOR_ID,
        ruleset_version=RULESET,
        event_id=event_id,
        event_type=(PROMISE_RETURNED_EVENT_V1 if policy_id is None else PROMISE_RETURNED_EVENT_V2),
        promise_id=promise.id,
        expected_promise_revision=promise.revision if revision is None else revision,
        promisor_id=promise.promisor_id,
        promisee_id=promise.promisee_id,
        effective_tick=tick,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=1,
        policy_id=policy_id,
        trust_cas=None if policy_id is None else cas_for(promise, bond),
        source_owner_id=promise.promisor_id,
        recipient_owner_id=promise.promisee_id,
        resource_id=promise.terms.resource_id,
        quantity=promise.terms.quantity,
    )


def breach_evidence(
    promise: RelationshipPromise,
    bond: PromiseTrustBondSnapshot | None,
    *,
    event_id: UUID = uid(81),
    policy_id: str | None = PROMISE_TRUST_POLICY_ID,
    tick: int | None = None,
    schedule_revision: int = 1,
) -> PromiseBreachEvidence:
    return PromiseBreachEvidence(
        outcome="breached",
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=uid(62),
        actor_id=uid(99),
        ruleset_version=RULESET,
        event_id=event_id,
        event_type=(PROMISE_BREACHED_EVENT_V1 if policy_id is None else PROMISE_BREACHED_EVENT_V2),
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        promisor_id=promise.promisor_id,
        promisee_id=promise.promisee_id,
        effective_tick=promise.deadline_tick + 1 if tick is None else tick,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=schedule_revision,
        policy_id=policy_id,
        trust_cas=None if policy_id is None else cas_for(promise, bond),
    )


def test_on_time_fulfillment_changes_only_directed_trust_and_spends_bounded_budget() -> None:
    promise = make_promise()
    bond = bond_snapshot(promise)
    evidence = fulfillment_evidence(promise, bond)
    plan = plan_promise_trust_effect(
        promise, evidence, bond, deadline_schedule(promise), command_context(tick=150),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )

    assert plan is not None
    assert (plan.from_id, plan.to_id) == (PROMISEE_ID, PROMISOR_ID)
    assert (plan.expected_relation_revision, plan.expected_previous_trust) == (3, Decimal("10"))
    assert (plan.applied_delta, plan.resulting_trust) == (Decimal("3"), Decimal("13"))
    assert plan.next_budget_policy_id == PROMISE_TRUST_POLICY_ID
    assert plan.next_success_budget_spent == Decimal("3")
    assert plan.evidence_refs_to_append == (evidence.event_id,)
    assert plan.receipt.policy_id == PROMISE_TRUST_POLICY_ID
    assert plan.receipt.outcome == "on_time_fulfilled"
    assert plan.receipt.applied_delta == Decimal("3")
    assert promise.status == "active" and promise.revision == 1
    assert bond.trust == Decimal("10") and bond.success_budget_spent == 0


def test_success_reward_budget_diminishes_and_never_exceeds_configured_cap() -> None:
    policy = PromiseTrustPolicy(policy_id="core.promise_trust.test.v1", success_budget_cap="5")
    promise1 = make_promise(trust_policy_id=policy.policy_id)
    bond1 = bond_snapshot(promise1, trust="0")
    plan1 = plan_promise_trust_effect(
        promise1, fulfillment_evidence(promise1, bond1, event_id=uid(82), policy_id=policy.policy_id), bond1,
        deadline_schedule(promise1), command_context(tick=150),
        promise_policy_id=policy.policy_id, policy=policy,
    )
    assert plan1 is not None and plan1.next_success_budget_spent == Decimal("3")

    promise2 = make_promise(promise_id=uid(41), trust_policy_id=policy.policy_id)
    bond2 = PromiseTrustBondSnapshot(
        relation_id=plan1.relation_id,
        from_id=PROMISEE_ID,
        to_id=PROMISOR_ID,
        revision=4,
        trust=plan1.resulting_trust,
        budget_policy_id=policy.policy_id,
        success_budget_spent=plan1.next_success_budget_spent,
        evidence_refs=plan1.evidence_refs_to_append,
    )
    plan2 = plan_promise_trust_effect(
        promise2, fulfillment_evidence(promise2, bond2, event_id=uid(83), command_id=uid(63), policy_id=policy.policy_id), bond2,
        deadline_schedule(promise2), command_context(command_id=uid(63), tick=150),
        promise_policy_id=policy.policy_id, policy=policy,
    )
    assert plan2 is not None
    assert (plan2.applied_delta, plan2.next_success_budget_spent) == (Decimal("2"), Decimal("5"))

    promise3 = make_promise(promise_id=uid(42), trust_policy_id=policy.policy_id)
    bond3 = PromiseTrustBondSnapshot(
        relation_id=plan2.relation_id,
        from_id=PROMISEE_ID,
        to_id=PROMISOR_ID,
        revision=5,
        trust=plan2.resulting_trust,
        budget_policy_id=policy.policy_id,
        success_budget_spent=plan2.next_success_budget_spent,
        evidence_refs=(plan1.outcome_event_id, plan2.outcome_event_id),
    )
    plan3 = plan_promise_trust_effect(
        promise3, fulfillment_evidence(promise3, bond3, event_id=uid(84), command_id=uid(64), policy_id=policy.policy_id), bond3,
        deadline_schedule(promise3), command_context(command_id=uid(64), tick=150),
        promise_policy_id=policy.policy_id, policy=policy,
    )
    assert plan3 is not None
    assert (plan3.applied_delta, plan3.resulting_trust, plan3.next_success_budget_spent) == (
        Decimal("0"), plan2.resulting_trust, Decimal("5"),
    )


def test_trust_clamps_at_bounds_but_budget_is_not_bankable() -> None:
    promise = make_promise()
    bond = bond_snapshot(promise, trust="99")
    plan = plan_promise_trust_effect(
        promise, fulfillment_evidence(promise, bond), bond, deadline_schedule(promise),
        command_context(tick=150), promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )
    assert plan is not None
    assert (plan.applied_delta, plan.resulting_trust, plan.next_success_budget_spent) == (
        Decimal("1"), Decimal("100"), Decimal("3"),
    )


def test_social_world_promise_outcomes_stop_at_key_threshold_and_do_not_move_high_scores() -> None:
    for index, (starting_trust, expected_trust) in enumerate(
        (("79.5", Decimal("79.999999")), ("80", Decimal("80"))),
        start=1,
    ):
        promise = make_promise(promise_id=uid(100 + index))
        bond = bond_snapshot(
            promise,
            trust=starting_trust,
            revision=3,
            budget_policy_id=PROMISE_TRUST_POLICY_ID,
        )
        plan = plan_promise_trust_effect(
            promise,
            fulfillment_evidence(promise, bond, event_id=uid(110 + index)),
            bond,
            deadline_schedule(promise),
            command_context(tick=150),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
            relationship_curve_enabled=True,
            routine_occurrence=1,
        )
        assert plan is not None
        assert plan.resulting_trust == expected_trust
        assert plan.resulting_trust < Decimal("80") or starting_trust == "80"
        assert plan.applied_delta == expected_trust - Decimal(starting_trust)

    for index, (starting_trust, expected_trust) in enumerate(
        (("-79.5", Decimal("-79.999999")), ("-80", Decimal("-80"))),
        start=1,
    ):
        promise = make_promise(promise_id=uid(120 + index))
        bond = bond_snapshot(
            promise,
            trust=starting_trust,
            revision=3,
            budget_policy_id=PROMISE_TRUST_POLICY_ID,
        )
        plan = plan_promise_trust_effect(
            promise,
            breach_evidence(promise, bond, event_id=uid(130 + index)),
            bond,
            deadline_schedule(promise),
            command_context(actor_id=uid(99), command_id=uid(62), tick=201),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
            relationship_curve_enabled=True,
        )
        assert plan is not None
        assert plan.resulting_trust == expected_trust
        assert plan.applied_delta == expected_trust - Decimal(starting_trust)

    for index, (starting_trust, expected_trust) in enumerate(
        (("79.5", Decimal("79.999999")), ("80", Decimal("80"))),
        start=1,
    ):
        breach_event_id = uid(140 + index)
        promise = make_promise(
            promise_id=uid(150 + index),
            status="breached",
            revision=2,
            resolved_tick=201,
            outcome_event_id=breach_event_id,
        )
        bond = bond_snapshot(
            promise,
            trust=starting_trust,
            revision=4,
            budget_policy_id=PROMISE_TRUST_POLICY_ID,
            evidence_refs=(breach_event_id,),
        )
        prior_receipt = PromiseTrustReceipt(
            policy_id=PROMISE_TRUST_POLICY_ID,
            promise_id=promise.id,
            outcome_event_id=breach_event_id,
            outcome="breached",
            applied_delta=Decimal("-8"),
        )
        late = PromiseLateReturnEvidence(
            outcome="late_return",
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uid(160 + index),
            actor_id=PROMISOR_ID,
            ruleset_version=RULESET,
            event_id=uid(170 + index),
            event_type=PROMISE_LATE_RETURN_EVENT_V1,
            promise_id=promise.id,
            expected_promise_revision=promise.revision,
            promisor_id=PROMISOR_ID,
            promisee_id=PROMISEE_ID,
            effective_tick=205,
            scheduled_event_id=promise.deadline_schedule_id,
            expected_schedule_revision=2,
            policy_id=PROMISE_TRUST_POLICY_ID,
            trust_cas=cas_for(promise, bond),
            source_owner_id=PROMISOR_ID,
            recipient_owner_id=PROMISEE_ID,
            resource_id=promise.terms.resource_id,
            quantity=promise.terms.quantity,
            source_breach_event_id=breach_event_id,
        )
        plan = plan_promise_trust_effect(
            promise,
            late,
            bond,
            deadline_schedule(promise, status="completed", revision=2),
            command_context(command_id=uid(160 + index), tick=205),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
            prior_receipt=prior_receipt,
            relationship_curve_enabled=True,
        )
        assert plan is not None
        assert plan.resulting_trust == expected_trust
        assert plan.applied_delta == expected_trust - Decimal(starting_trust)


def test_repeated_routine_fulfillments_cannot_max_trust() -> None:
    policy = PromiseTrustPolicy(on_time_reward="1", success_budget_cap="100")
    bond = None
    trust = Decimal("0")

    for occurrence in range(1, 101):
        promise = make_promise(promise_id=uid(200 + occurrence), trust_policy_id=policy.policy_id)
        evidence = fulfillment_evidence(
            promise,
            bond,
            event_id=uid(300 + occurrence),
            command_id=uid(400 + occurrence),
            policy_id=policy.policy_id,
        )
        plan = plan_promise_trust_effect(
            promise,
            evidence,
            bond,
            deadline_schedule(promise),
            command_context(command_id=uid(400 + occurrence), tick=150),
            promise_policy_id=policy.policy_id,
            policy=policy,
            relationship_curve_enabled=True,
            routine_occurrence=occurrence,
        )
        assert plan is not None
        assert plan.resulting_trust < Decimal("80")
        trust = plan.resulting_trust
        bond = PromiseTrustBondSnapshot(
            relation_id=plan.relation_id,
            from_id=plan.from_id,
            to_id=plan.to_id,
            revision=occurrence,
            trust=plan.resulting_trust,
            budget_policy_id=plan.next_budget_policy_id,
            success_budget_spent=plan.next_success_budget_spent,
            evidence_refs=(() if bond is None else bond.evidence_refs) + (evidence.event_id,),
        )

    assert Decimal("1.99") < trust < Decimal("2.01")


def test_new_directed_bond_uses_stable_promisee_to_promisor_id() -> None:
    promise = make_promise()
    evidence = fulfillment_evidence(promise, None)
    plan = plan_promise_trust_effect(
        promise, evidence, None, deadline_schedule(promise), command_context(tick=150),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )
    assert plan is not None
    assert plan.relation_id == promise_trust_relation_id(WORLD_ID, PROMISEE_ID, PROMISOR_ID)
    assert plan.expected_relation_revision is None
    assert plan.expected_previous_trust is None
    assert plan.resulting_trust == Decimal("3")


def test_scheduled_breach_reduces_trust_and_records_a_sanction_receipt() -> None:
    promise = make_promise()
    bond = bond_snapshot(promise, trust="10")
    evidence = breach_evidence(promise, bond)
    schedule = deadline_schedule(promise)
    plan = plan_promise_trust_effect(
        promise, evidence, bond, schedule, command_context(actor_id=uid(99), command_id=uid(62), tick=201),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )

    assert plan is not None
    assert (plan.outcome, plan.applied_delta, plan.resulting_trust) == (
        "breached", Decimal("-8"), Decimal("2"),
    )
    assert plan.receipt.outcome_event_id == evidence.event_id
    assert plan.receipt.applied_delta == Decimal("-8")
    assert plan.receipt.late_return_event_id is None


def test_late_return_repairs_only_part_of_the_actual_breach_loss_and_keeps_breach_receipt() -> None:
    promise = make_promise()
    before = bond_snapshot(promise, trust="10")
    breach = plan_promise_trust_effect(
        promise, breach_evidence(promise, before), before, deadline_schedule(promise),
        command_context(actor_id=uid(99), command_id=uid(62), tick=201), promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )
    assert breach is not None

    breached_promise = make_promise(
        status="breached",
        revision=2,
        resolved_tick=201,
        outcome_event_id=breach.outcome_event_id,
    )
    after_breach = PromiseTrustBondSnapshot(
        relation_id=breach.relation_id,
        from_id=PROMISEE_ID,
        to_id=PROMISOR_ID,
        revision=4,
        trust=breach.resulting_trust,
        budget_policy_id=PROMISE_TRUST_POLICY_ID,
        success_budget_spent=breach.next_success_budget_spent,
        evidence_refs=(breach.outcome_event_id,),
    )
    late = PromiseLateReturnEvidence(
        outcome="late_return",
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=uid(65),
        actor_id=PROMISOR_ID,
        ruleset_version=RULESET,
        event_id=uid(85),
        event_type=PROMISE_LATE_RETURN_EVENT_V1,
        promise_id=breached_promise.id,
        expected_promise_revision=breached_promise.revision,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        effective_tick=205,
        scheduled_event_id=breached_promise.deadline_schedule_id,
        expected_schedule_revision=2,
        policy_id=PROMISE_TRUST_POLICY_ID,
        trust_cas=cas_for(breached_promise, after_breach),
        source_owner_id=PROMISOR_ID,
        recipient_owner_id=PROMISEE_ID,
        resource_id=breached_promise.terms.resource_id,
        quantity=breached_promise.terms.quantity,
        source_breach_event_id=breach.outcome_event_id,
    )
    late_plan = plan_promise_trust_effect(
        breached_promise,
        late,
        after_breach,
        deadline_schedule(breached_promise, status="completed", revision=2),
        command_context(command_id=uid(65), tick=205),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
        prior_receipt=breach.receipt,
    )

    assert late_plan is not None
    assert late_plan.applied_delta == Decimal("2")
    assert late_plan.resulting_trust == Decimal("4")
    assert late_plan.receipt.outcome == "breached"
    assert late_plan.receipt.outcome_event_id == breach.outcome_event_id
    assert late_plan.receipt.applied_delta == Decimal("-8")
    assert late_plan.receipt.late_return_event_id == late.event_id
    assert late_plan.receipt.late_return_delta == Decimal("2")
    assert late_plan.evidence_refs_to_append == (breach.outcome_event_id, late.event_id)
    assert breached_promise.status == "breached"
    assert breached_promise.outcome_event_id == breach.outcome_event_id

    detached_bond = after_breach.model_copy(update={"evidence_refs": ()})
    detached_late = late.model_copy(update={"trust_cas": cas_for(breached_promise, detached_bond)})
    with pytest.raises(InvalidEvent, match="no committed reference to the original breach"):
        plan_promise_trust_effect(
            breached_promise,
            detached_late,
            detached_bond,
            deadline_schedule(breached_promise, status="completed", revision=2),
            command_context(command_id=uid(65), tick=205),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
            prior_receipt=breach.receipt,
        )

    with pytest.raises(InvalidEvent, match="at most one trust repair"):
        plan_promise_trust_effect(
            breached_promise,
            late.model_copy(update={"event_id": uid(86), "command_id": uid(66)}),
            after_breach,
            deadline_schedule(breached_promise, status="completed", revision=2),
            command_context(command_id=uid(66), tick=205),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
            prior_receipt=late_plan.receipt,
        )


def test_late_repair_is_capped_by_actual_clamped_loss() -> None:
    promise = make_promise()
    bond = bond_snapshot(promise, trust="-99")
    breach = plan_promise_trust_effect(
        promise, breach_evidence(promise, bond), bond, deadline_schedule(promise),
        command_context(actor_id=uid(99), command_id=uid(62), tick=201), promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )
    assert breach is not None
    assert breach.applied_delta == Decimal("-1")

    breached_promise = make_promise(status="breached", revision=2, resolved_tick=201, outcome_event_id=breach.outcome_event_id)
    after_breach = PromiseTrustBondSnapshot(
        relation_id=breach.relation_id,
        from_id=PROMISEE_ID,
        to_id=PROMISOR_ID,
        revision=4,
        trust=breach.resulting_trust,
        budget_policy_id=PROMISE_TRUST_POLICY_ID,
        evidence_refs=(breach.outcome_event_id,),
    )
    late = PromiseLateReturnEvidence(
        outcome="late_return", world_id=WORLD_ID, branch_id=BRANCH_ID,
        command_id=uid(67), actor_id=PROMISOR_ID, ruleset_version=RULESET,
        event_id=uid(87), event_type=PROMISE_LATE_RETURN_EVENT_V1,
        promise_id=promise.id, expected_promise_revision=2,
        promisor_id=PROMISOR_ID, promisee_id=PROMISEE_ID,
        effective_tick=202, scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=2, policy_id=PROMISE_TRUST_POLICY_ID,
        trust_cas=cas_for(breached_promise, after_breach),
        source_owner_id=PROMISOR_ID, recipient_owner_id=PROMISEE_ID,
        resource_id=promise.terms.resource_id, quantity=promise.terms.quantity,
        source_breach_event_id=breach.outcome_event_id,
    )
    repaired = plan_promise_trust_effect(
        breached_promise, late, after_breach,
        deadline_schedule(breached_promise, status="completed", revision=2),
        command_context(command_id=uid(67), tick=202), promise_policy_id=PROMISE_TRUST_POLICY_ID,
        prior_receipt=breach.receipt,
    )
    assert repaired is not None
    assert (repaired.applied_delta, repaired.resulting_trust) == (Decimal("1"), Decimal("-99"))
    assert repaired.receipt.late_return_delta == Decimal("1")


def test_release_and_legacy_v1_outcomes_have_no_trust_effect() -> None:
    promise = make_promise()
    bond = bond_snapshot(promise)
    release = PromiseReleaseEvidence(
        outcome="released", world_id=WORLD_ID, branch_id=BRANCH_ID,
        command_id=uid(68), actor_id=PROMISEE_ID, ruleset_version=RULESET,
        event_id=uid(88), event_type=PROMISE_RELEASED_EVENT_V1,
        promise_id=promise.id, expected_promise_revision=1,
        promisor_id=PROMISOR_ID, promisee_id=PROMISEE_ID,
        effective_tick=150, scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=1, policy_id=PROMISE_TRUST_POLICY_ID,
    )
    assert plan_promise_trust_effect(
        promise, release, bond, deadline_schedule(promise), command_context(actor_id=PROMISEE_ID, command_id=uid(68)),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
    ) is None

    legacy_promise = make_promise(trust_policy_id=None)
    legacy_bond = bond_snapshot(legacy_promise)
    legacy_evidence = fulfillment_evidence(legacy_promise, None, policy_id=None, event_id=uid(89), tick=150)
    assert legacy_evidence.event_type == PROMISE_RETURNED_EVENT_V1
    assert plan_promise_trust_effect(
        legacy_promise, legacy_evidence, legacy_bond, deadline_schedule(legacy_promise, policy_enabled=False),
        command_context(tick=150), promise_policy_id=None,
    ) is None


@pytest.mark.parametrize(
    "change",
    [
        {"expected_promise_revision": 2},
        {"promisor_id": uid(90)},
        {"recipient_owner_id": uid(90)},
        {"resource_id": "cultivation.iron"},
        {"quantity": Decimal("1")},
        {"effective_tick": 201},
        {"scheduled_event_id": uid(90)},
    ],
)
def test_fulfillment_authorization_rejects_changed_promise_terms_or_revision(change) -> None:
    promise = make_promise()
    bond = bond_snapshot(promise)
    evidence = fulfillment_evidence(promise, bond).model_copy(update=change)
    context_tick = evidence.effective_tick
    with pytest.raises(InvalidEvent):
        plan_promise_trust_effect(
            promise, evidence, bond, deadline_schedule(promise), command_context(tick=context_tick),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )


def test_stale_context_schedule_and_relation_cas_are_rejected() -> None:
    promise = make_promise()
    bond = bond_snapshot(promise)
    evidence = fulfillment_evidence(promise, bond)
    with pytest.raises(InvalidEvent, match="trusted command and current tick"):
        plan_promise_trust_effect(
            promise, evidence, bond, deadline_schedule(promise), command_context(command_id=uid(999), tick=150),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )

    stale_cas = evidence.model_copy(update={"trust_cas": evidence.trust_cas.model_copy(update={"expected_previous_trust": Decimal("9")})})
    with pytest.raises(InvalidEvent, match="compare-and-swap"):
        plan_promise_trust_effect(
            promise, stale_cas, bond, deadline_schedule(promise), command_context(tick=150),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )

    stale_schedule = deadline_schedule(promise, revision=2)
    with pytest.raises(InvalidEvent, match="schedule compare-and-swap"):
        plan_promise_trust_effect(
            promise, evidence, bond, stale_schedule, command_context(tick=150),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )

    duplicate = fulfillment_evidence(promise, bond, event_id=uid(80))
    seen = bond.model_copy(update={"evidence_refs": (uid(80),)})
    with pytest.raises(InvalidEvent, match="already affected"):
        plan_promise_trust_effect(
            promise, duplicate, seen, deadline_schedule(promise), command_context(tick=150),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )


def test_wrong_bond_direction_policy_or_missing_late_receipt_is_rejected() -> None:
    promise = make_promise()
    wrong_direction = PromiseTrustBondSnapshot(
        relation_id=uid(70), from_id=PROMISOR_ID, to_id=PROMISEE_ID, revision=1, trust="0",
    )
    evidence = fulfillment_evidence(promise, wrong_direction)
    with pytest.raises(InvalidEvent, match="directed promisee-to-promisor"):
        plan_promise_trust_effect(
            promise, evidence, wrong_direction, deadline_schedule(promise), command_context(tick=150),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )

    with pytest.raises(InvalidEvent, match="authored promise policy"):
        plan_promise_trust_effect(
            promise, evidence.model_copy(update={"policy_id": "core.promise_trust.other"}),
            bond_snapshot(promise), deadline_schedule(promise), command_context(tick=150),
            promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )

    breached = make_promise(status="breached", revision=2, resolved_tick=201, outcome_event_id=uid(91))
    late = PromiseLateReturnEvidence(
        outcome="late_return", world_id=WORLD_ID, branch_id=BRANCH_ID,
        command_id=uid(69), actor_id=PROMISOR_ID, ruleset_version=RULESET,
        event_id=uid(92), event_type=PROMISE_LATE_RETURN_EVENT_V1,
        promise_id=breached.id, expected_promise_revision=2,
        promisor_id=PROMISOR_ID, promisee_id=PROMISEE_ID,
        effective_tick=202, scheduled_event_id=breached.deadline_schedule_id,
        expected_schedule_revision=2, policy_id=PROMISE_TRUST_POLICY_ID,
        trust_cas=cas_for(breached, bond_snapshot(breached, trust="2", revision=4, budget_policy_id=PROMISE_TRUST_POLICY_ID)),
        source_owner_id=PROMISOR_ID, recipient_owner_id=PROMISEE_ID,
        resource_id=breached.terms.resource_id, quantity=breached.terms.quantity,
        source_breach_event_id=uid(91),
    )
    with pytest.raises(InvalidEvent, match="receipt from the original breach"):
        plan_promise_trust_effect(
            breached, late, bond_snapshot(breached, trust="2", revision=4, budget_policy_id=PROMISE_TRUST_POLICY_ID),
            deadline_schedule(breached, status="completed", revision=2),
            command_context(command_id=uid(69), tick=202), promise_policy_id=PROMISE_TRUST_POLICY_ID,
        )


def test_policy_defaults_are_bounded_and_versioned() -> None:
    assert DEFAULT_PROMISE_TRUST_POLICY.policy_id == PROMISE_TRUST_POLICY_ID
    assert DEFAULT_PROMISE_TRUST_POLICY.on_time_reward == Decimal("3")
    assert DEFAULT_PROMISE_TRUST_POLICY.breach_penalty == Decimal("-8")
    assert DEFAULT_PROMISE_TRUST_POLICY.late_return_repair == Decimal("2")
    assert DEFAULT_PROMISE_TRUST_POLICY.success_budget_cap == Decimal("6")
    with pytest.raises(ValidationError):
        PromiseTrustPolicy(success_budget_cap="101")
    with pytest.raises(ValidationError, match="smaller than"):
        PromiseTrustPolicy(breach_penalty="-2", late_return_repair="2")
