from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.promise_trust import (
    PROMISE_BREACHED_EVENT_V2,
    PROMISE_LATE_RETURN_EVENT_V1,
    PROMISE_RETURNED_EVENT_V2,
    PROMISE_TRUST_DEADLINE_HANDLER_VERSION,
    PROMISE_TRUST_POLICY_ID,
    PromiseBreachEvidence,
    PromiseFulfillmentEvidence,
    PromiseLateReturnEvidence,
    PromiseTrustBondSnapshot,
    PromiseTrustCas,
    PromiseTrustCommandContext,
    plan_promise_trust_effect,
    promise_trust_relation_id,
)
from world_engine.domain.promise_trust_state import (
    PromiseTrustBond,
    PromiseTrustProjection,
    apply_promise_trust_plan,
    verify_promise_trust_projection,
)
from world_engine.domain.relationship_promises import (
    PROMISE_DEADLINE_HANDLER,
    RelationshipPromise,
    ReturnItemTerms,
    create_return_promise,
)


WORLD_ID = UUID(int=1000)
BRANCH_ID = UUID(int=1001)
OTHER_BRANCH_ID = UUID(int=1002)
RULESET_VERSION = "rules.promise_trust_projection.test.v1"
PROMISOR_ID = UUID(int=11)
PROMISEE_ID = UUID(int=12)
SYSTEM_ACTOR_ID = UUID(int=99)


def uid(value: int) -> UUID:
    return UUID(int=value)


def make_promise(*, promise_id: UUID = uid(40), deadline_tick: int = 100) -> RelationshipPromise:
    promise = create_return_promise(
        promise_id=promise_id,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        private_text="我会按约把两份药材还给你。",
        terms=ReturnItemTerms(
            resource_id="cultivation.herb",
            quantity="2",
            promisee_account_id=uid(22),
        ),
        created_tick=10,
        deadline_tick=deadline_tick,
    )
    return RelationshipPromise.model_validate({
        **promise.model_dump(mode="python"),
        "trust_policy_id": PROMISE_TRUST_POLICY_ID,
    })


def command_context(*, command_id: UUID, actor_id: UUID, tick: int, branch_id: UUID = BRANCH_ID):
    return PromiseTrustCommandContext(
        world_id=WORLD_ID,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=RULESET_VERSION,
        current_tick=tick,
    )


def deadline_schedule(
    promise: RelationshipPromise,
    *,
    status: str = "queued",
    revision: int = 1,
) -> ScheduledEventRecord:
    return ScheduledEventRecord(
        id=promise.deadline_schedule_id,
        due_tick=promise.deadline_tick + 1,
        phase="maintenance",
        handler_id=PROMISE_DEADLINE_HANDLER,
        handler_version=PROMISE_TRUST_DEADLINE_HANDLER_VERSION,
        status=status,
        max_runs=1,
        completed_runs=1 if status == "completed" else 0,
        revision=revision,
        payload={"promise_id": str(promise.id)},
    )


def bond_snapshot(
    projection: PromiseTrustProjection,
    promise: RelationshipPromise,
) -> PromiseTrustBondSnapshot | None:
    bond_id = promise_trust_relation_id(WORLD_ID, promise.promisee_id, promise.promisor_id)
    bond = projection.bonds.get(bond_id)
    if bond is None:
        return None
    return PromiseTrustBondSnapshot(
        relation_id=bond.id,
        from_id=bond.from_id,
        to_id=bond.to_id,
        revision=bond.revision,
        trust=bond.trust,
        budget_policy_id=bond.policy_id,
        success_budget_spent=bond.success_budget_spent,
        evidence_refs=bond.evidence_refs,
    )


def fulfillment_plan(
    projection: PromiseTrustProjection,
    promise: RelationshipPromise,
    *,
    event_id: UUID = uid(80),
    command_id: UUID = uid(81),
    tick: int = 50,
    branch_id: UUID = BRANCH_ID,
):
    bond = bond_snapshot(projection, promise)
    evidence = PromiseFulfillmentEvidence(
        outcome="on_time_fulfilled",
        world_id=WORLD_ID,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=PROMISOR_ID,
        ruleset_version=RULESET_VERSION,
        event_id=event_id,
        event_type=PROMISE_RETURNED_EVENT_V2,
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        effective_tick=tick,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=1,
        policy_id=PROMISE_TRUST_POLICY_ID,
        trust_cas=_cas_fields(bond, promise),
        source_owner_id=PROMISOR_ID,
        recipient_owner_id=PROMISEE_ID,
        resource_id=promise.terms.resource_id,
        quantity=promise.terms.quantity,
    )
    return plan_promise_trust_effect(
        promise,
        evidence,
        bond,
        deadline_schedule(promise),
        command_context(command_id=command_id, actor_id=PROMISOR_ID, tick=tick, branch_id=branch_id),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )


def _cas_fields(
    snapshot: PromiseTrustBondSnapshot | None,
    promise: RelationshipPromise,
) -> PromiseTrustCas:
    return PromiseTrustCas(
        relation_id=(
            snapshot.relation_id if snapshot is not None and snapshot.relation_id is not None
            else promise_trust_relation_id(WORLD_ID, promise.promisee_id, promise.promisor_id)
        ),
        expected_relation_revision=snapshot.revision if snapshot is not None else None,
        expected_previous_trust=snapshot.trust if snapshot is not None else None,
        expected_budget_policy_id=snapshot.budget_policy_id if snapshot is not None else None,
        expected_success_budget_spent=snapshot.success_budget_spent if snapshot is not None else Decimal("0"),
    )


def test_first_effect_creates_isolated_directed_bond_from_zero_and_freezes_maps():
    promise = make_promise()
    projection = PromiseTrustProjection.empty(WORLD_ID, BRANCH_ID)
    plan = fulfillment_plan(projection, promise)

    assert plan is not None
    updated = apply_promise_trust_plan(
        projection, plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
    )
    bond_id = promise_trust_relation_id(WORLD_ID, PROMISEE_ID, PROMISOR_ID)
    bond = updated.bonds[bond_id]
    assert bond.world_id == WORLD_ID
    assert not hasattr(bond, "branch_id")
    assert (bond.from_id, bond.to_id) == (PROMISEE_ID, PROMISOR_ID)
    assert (bond.revision, bond.trust) == (1, Decimal("3"))
    assert bond.evidence_refs == (plan.outcome_event_id,)
    assert updated.receipts[promise.id] == plan.receipt
    assert projection.bonds == {} and projection.receipts == {}
    with pytest.raises(TypeError):
        updated.bonds[bond_id] = bond  # type: ignore[index]


def test_duplicate_stale_and_cross_branch_plans_are_rejected():
    promise = make_promise()
    empty = PromiseTrustProjection.empty(WORLD_ID, BRANCH_ID)
    plan = fulfillment_plan(empty, promise)
    assert plan is not None
    committed = apply_promise_trust_plan(
        empty, plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
    )

    with pytest.raises(InvalidEvent, match="compare-and-swap"):
        apply_promise_trust_plan(
            committed, plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
        )

    other_branch = PromiseTrustProjection(
        world_id=WORLD_ID,
        branch_id=OTHER_BRANCH_ID,
        bonds=committed.bonds,
        receipts=committed.receipts,
    )
    with pytest.raises(InvalidEvent, match="another world or branch"):
        verify_promise_trust_projection(
            other_branch,
            expected_world_id=WORLD_ID,
            expected_branch_id=BRANCH_ID,
        )
    with pytest.raises(InvalidEvent, match="another world or branch"):
        apply_promise_trust_plan(
            other_branch, plan, expected_world_id=WORLD_ID, expected_branch_id=OTHER_BRANCH_ID,
        )


def test_projection_verifier_requires_receipt_refs_to_be_unique_and_bonded():
    promise = make_promise()
    projection = PromiseTrustProjection.empty(WORLD_ID, BRANCH_ID)
    plan = fulfillment_plan(projection, promise)
    assert plan is not None
    committed = apply_promise_trust_plan(
        projection, plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
    )
    verify_promise_trust_projection(
        committed,
        expected_world_id=WORLD_ID,
        expected_branch_id=BRANCH_ID,
    )

    no_receipt = PromiseTrustProjection(
        WORLD_ID, BRANCH_ID, bonds=committed.bonds, receipts={},
    )
    with pytest.raises(InvalidEvent, match="without matching promise receipts"):
        verify_promise_trust_projection(
            no_receipt,
            expected_world_id=WORLD_ID,
            expected_branch_id=BRANCH_ID,
        )

    no_bond = PromiseTrustProjection(
        WORLD_ID, BRANCH_ID, bonds={}, receipts=committed.receipts,
    )
    with pytest.raises(InvalidEvent, match="no matching bond audit reference"):
        verify_promise_trust_projection(
            no_bond,
            expected_world_id=WORLD_ID,
            expected_branch_id=BRANCH_ID,
        )

    receipt = committed.receipts[promise.id]
    repeated_receipt_id = uid(47)
    duplicate_receipt = receipt.model_copy(update={"promise_id": repeated_receipt_id})
    duplicate_receipts = PromiseTrustProjection(
        WORLD_ID,
        BRANCH_ID,
        bonds=committed.bonds,
        receipts={promise.id: receipt, repeated_receipt_id: duplicate_receipt},
    )
    with pytest.raises(InvalidEvent, match="referenced by more than one promise receipt"):
        verify_promise_trust_projection(
            duplicate_receipts,
            expected_world_id=WORLD_ID,
            expected_branch_id=BRANCH_ID,
        )

    bond = next(iter(committed.bonds.values()))
    duplicate_bond = PromiseTrustBond.model_validate({
        **bond.model_dump(mode="python"),
        "id": promise_trust_relation_id(WORLD_ID, uid(31), uid(32)),
        "from_id": uid(31),
        "to_id": uid(32),
        "evidence_refs": bond.evidence_refs,
    })
    duplicate_refs = PromiseTrustProjection(
        WORLD_ID,
        BRANCH_ID,
        bonds={bond.id: bond, duplicate_bond.id: duplicate_bond},
        receipts=committed.receipts,
    )
    with pytest.raises(InvalidEvent, match="appears more than once across bonds"):
        verify_promise_trust_projection(
            duplicate_refs,
            expected_world_id=WORLD_ID,
            expected_branch_id=BRANCH_ID,
        )


def test_late_return_appends_audit_and_repairs_without_erasing_breach_sanction():
    promise = make_promise()
    projection = PromiseTrustProjection.empty(WORLD_ID, BRANCH_ID)
    breach_id = uid(90)
    breach_command_id = uid(91)
    breach = PromiseBreachEvidence(
        outcome="breached",
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=breach_command_id,
        actor_id=SYSTEM_ACTOR_ID,
        ruleset_version=RULESET_VERSION,
        event_id=breach_id,
        event_type=PROMISE_BREACHED_EVENT_V2,
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        effective_tick=promise.deadline_tick + 1,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=1,
        policy_id=PROMISE_TRUST_POLICY_ID,
        trust_cas=_cas_fields(None, promise),
    )
    breach_plan = plan_promise_trust_effect(
        promise,
        breach,
        None,
        deadline_schedule(promise),
        command_context(command_id=breach_command_id, actor_id=SYSTEM_ACTOR_ID, tick=101),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
    )
    assert breach_plan is not None
    after_breach = apply_promise_trust_plan(
        projection, breach_plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
    )
    assert after_breach.bonds[breach_plan.relation_id].trust == Decimal("-8")
    assert after_breach.receipts[promise.id].applied_delta == Decimal("-8")

    breached_promise = RelationshipPromise.model_validate({
        **promise.model_dump(mode="python"),
        "status": "breached",
        "resolved_tick": 101,
        "revision": 2,
        "outcome_event_id": breach_id,
    })
    snapshot = bond_snapshot(after_breach, breached_promise)
    assert snapshot is not None
    late_command_id = uid(92)
    late_event_id = uid(93)
    late = PromiseLateReturnEvidence(
        outcome="late_return",
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=late_command_id,
        actor_id=PROMISOR_ID,
        ruleset_version=RULESET_VERSION,
        event_id=late_event_id,
        event_type=PROMISE_LATE_RETURN_EVENT_V1,
        promise_id=promise.id,
        expected_promise_revision=breached_promise.revision,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        effective_tick=105,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=2,
        policy_id=PROMISE_TRUST_POLICY_ID,
        trust_cas=_cas_fields(snapshot, breached_promise),
        source_owner_id=PROMISOR_ID,
        recipient_owner_id=PROMISEE_ID,
        resource_id=promise.terms.resource_id,
        quantity=promise.terms.quantity,
        source_breach_event_id=breach_id,
    )
    late_plan = plan_promise_trust_effect(
        breached_promise,
        late,
        snapshot,
        deadline_schedule(promise, status="completed", revision=2),
        command_context(command_id=late_command_id, actor_id=PROMISOR_ID, tick=105),
        promise_policy_id=PROMISE_TRUST_POLICY_ID,
        prior_receipt=after_breach.receipts[promise.id],
    )
    assert late_plan is not None

    repaired = apply_promise_trust_plan(
        after_breach, late_plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
    )
    assert repaired.bonds[late_plan.relation_id].trust == Decimal("-6")
    assert repaired.bonds[late_plan.relation_id].evidence_refs == (breach_id, late_event_id)
    receipt = repaired.receipts[promise.id]
    assert receipt.outcome == "breached"
    assert receipt.outcome_event_id == breach_id
    assert receipt.applied_delta == Decimal("-8")
    assert receipt.late_return_event_id == late_event_id
    assert receipt.late_return_delta == Decimal("2")

    with pytest.raises(InvalidEvent, match="compare-and-swap"):
        apply_promise_trust_plan(
            repaired, late_plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
        )


def test_projection_round_trip_preserves_bonds_and_receipt_map_with_host_branch_context():
    promise = make_promise()
    projection = PromiseTrustProjection.empty(WORLD_ID, BRANCH_ID)
    plan = fulfillment_plan(projection, promise)
    assert plan is not None
    committed = apply_promise_trust_plan(
        projection, plan, expected_world_id=WORLD_ID, expected_branch_id=BRANCH_ID,
    )

    restored = PromiseTrustProjection.from_payload(committed.to_payload(), branch_id=BRANCH_ID)
    assert restored.world_id == committed.world_id
    assert restored.branch_id == committed.branch_id
    assert restored.bonds == committed.bonds
    assert restored.receipts == committed.receipts


def test_fork_inherits_byte_identical_projection_and_applies_child_branch_plan():
    parent_promise = make_promise()
    parent_projection = PromiseTrustProjection.empty(WORLD_ID, BRANCH_ID)
    parent_plan = fulfillment_plan(
        parent_projection, parent_promise, event_id=uid(110), command_id=uid(111),
    )
    assert parent_plan is not None
    parent_projection = apply_promise_trust_plan(
        parent_projection,
        parent_plan,
        expected_world_id=WORLD_ID,
        expected_branch_id=BRANCH_ID,
    )
    durable_payload = parent_projection.to_payload()
    assert "branch_id" not in durable_payload
    assert all("branch_id" not in bond_payload for bond_payload in durable_payload["bonds"].values())

    # No branch context is persisted. The child can load the same bytes and
    # bind them to its current branch through trusted host context.
    child_projection = PromiseTrustProjection.from_payload(durable_payload)
    assert child_projection.branch_id is None
    verify_promise_trust_projection(
        child_projection,
        expected_world_id=WORLD_ID,
        expected_branch_id=OTHER_BRANCH_ID,
    )
    child_promise = make_promise(promise_id=uid(41))
    child_plan = fulfillment_plan(
        child_projection,
        child_promise,
        event_id=uid(112),
        command_id=uid(113),
        branch_id=OTHER_BRANCH_ID,
    )
    assert child_plan is not None

    with pytest.raises(InvalidEvent, match="another world or branch projection"):
        apply_promise_trust_plan(
            child_projection,
            parent_plan,
            expected_world_id=WORLD_ID,
            expected_branch_id=OTHER_BRANCH_ID,
        )

    child_updated = apply_promise_trust_plan(
        child_projection,
        child_plan,
        expected_world_id=WORLD_ID,
        expected_branch_id=OTHER_BRANCH_ID,
    )
    child_bond = child_updated.bonds[child_plan.relation_id]
    assert child_bond.revision == 2
    assert child_bond.trust == Decimal("6")
    assert child_updated.branch_id == OTHER_BRANCH_ID
    assert child_updated.to_payload()["world_id"] == durable_payload["world_id"]


