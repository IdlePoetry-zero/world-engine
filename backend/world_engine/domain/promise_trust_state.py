"""Immutable world-scoped projections for private promise trust effects.

These records are deliberately separate from public ``RelationRecord`` values:
they accumulate only the trust contribution created by authored promise policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from .components import ExactDecimal, ResourceName, SignedRelationValue
from .errors import InvalidEvent
from .promise_trust import (
    PromiseTrustModel,
    PromiseTrustPlan,
    PromiseTrustReceipt,
    promise_trust_relation_id,
)

_MAX_BUDGET = Decimal("100")
_MIN_TRUST = Decimal("-100")
_MAX_TRUST = Decimal("100")


class PromiseTrustBond(PromiseTrustModel):
    """One directed promise-trust contribution, inherited unchanged by forks."""

    id: UUID
    world_id: UUID
    from_id: UUID
    to_id: UUID
    revision: StrictInt = Field(ge=1)
    trust: SignedRelationValue
    policy_id: ResourceName
    success_budget_spent: ExactDecimal = Field(ge=Decimal("0"), le=_MAX_BUDGET)
    evidence_refs: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def bond_identity_and_audit_are_valid(self) -> PromiseTrustBond:
        if self.from_id == self.to_id:
            raise ValueError("promise trust bond endpoints must be distinct")
        if self.id != promise_trust_relation_id(self.world_id, self.from_id, self.to_id):
            raise ValueError("promise trust bond ID must be derived from its directed world endpoints")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("promise trust bond audit references must be unique")
        return self


@dataclass(frozen=True, slots=True)
class PromiseTrustProjection:
    """Read-only projection with optional in-memory branch binding.

    Branch identity is host context and deliberately does not enter the
    durable payload, so a fork inherits the same projection bytes.
    """

    world_id: UUID
    branch_id: UUID | None = None
    bonds: Mapping[UUID, PromiseTrustBond] = field(default_factory=dict)
    receipts: Mapping[UUID, PromiseTrustReceipt] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bonds", MappingProxyType(dict(self.bonds)))
        object.__setattr__(self, "receipts", MappingProxyType(dict(self.receipts)))

    @classmethod
    def empty(cls, world_id: UUID, branch_id: UUID) -> PromiseTrustProjection:
        return cls(world_id=world_id, branch_id=branch_id)

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-ready payload for durable storage."""

        return {
            "world_id": str(self.world_id),
            "bonds": {
                str(bond_id): bond.model_dump(mode="json")
                for bond_id, bond in self.bonds.items()
            },
            "receipts": {
                str(promise_id): receipt.model_dump(mode="json")
                for promise_id, receipt in self.receipts.items()
            },
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        branch_id: UUID | None = None,
    ) -> PromiseTrustProjection:
        if set(payload) != {"world_id", "bonds", "receipts"}:
            raise InvalidEvent("Promise trust projection payload has missing or unknown fields.")
        try:
            world_id = UUID(str(payload["world_id"]))
            raw_bonds = payload["bonds"]
            raw_receipts = payload["receipts"]
            if not isinstance(raw_bonds, Mapping) or not isinstance(raw_receipts, Mapping):
                raise TypeError
            bonds = {
                UUID(str(bond_id)): PromiseTrustBond.model_validate(value)
                for bond_id, value in raw_bonds.items()
            }
            receipts = {
                UUID(str(promise_id)): PromiseTrustReceipt.model_validate(value)
                for promise_id, value in raw_receipts.items()
            }
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("Promise trust projection payload is invalid.") from exc
        projection = cls(world_id=world_id, branch_id=branch_id, bonds=bonds, receipts=receipts)
        verify_promise_trust_projection(
            projection,
            expected_world_id=world_id,
            expected_branch_id=branch_id,
        )
        return projection


def apply_promise_trust_plan(
    projection: PromiseTrustProjection,
    plan: PromiseTrustPlan,
    *,
    expected_world_id: UUID,
    expected_branch_id: UUID,
) -> PromiseTrustProjection:
    """CAS-apply one trusted effect plan under explicit world/branch context.

    Initial fulfillment/breach plans add one bond and one promise receipt. A
    late return appends its event to the same directed bond and updates the
    original breach receipt in place while retaining that breach sanction.
    """

    projection = _validated_projection(projection)
    plan = PromiseTrustPlan.model_validate(plan.model_dump(mode="python"))
    verify_promise_trust_projection(
        projection,
        expected_world_id=expected_world_id,
        expected_branch_id=expected_branch_id,
    )
    if (
        expected_world_id != projection.world_id
        or plan.world_id != expected_world_id
        or plan.branch_id != expected_branch_id
    ):
        raise InvalidEvent("Promise trust plan belongs to another world or branch projection.")
    if plan.from_id == plan.to_id or plan.relation_id != promise_trust_relation_id(
        projection.world_id, plan.from_id, plan.to_id
    ):
        raise InvalidEvent("Promise trust plan has an invalid directed bond identity.")
    if plan.receipt.promise_id != plan.promise_id or plan.receipt.policy_id != plan.policy_id:
        raise InvalidEvent("Promise trust plan receipt does not match its promise and authored policy.")

    bond = projection.bonds.get(plan.relation_id)
    _validate_plan_cas(plan, bond)
    _validate_plan_values(plan, bond)

    refs = plan.evidence_refs_to_append
    if len(refs) != len(set(refs)):
        raise InvalidEvent("Promise trust plan contains duplicate audit references.")
    receipt_values = dict(projection.receipts)
    all_refs = {reference for existing in projection.bonds.values() for reference in existing.evidence_refs}
    receipt = receipt_values.get(plan.promise_id)

    if plan.outcome in {"on_time_fulfilled", "breached"}:
        if receipt is not None:
            raise InvalidEvent("Promise trust outcome already has a per-promise receipt.")
        if refs != (plan.outcome_event_id,):
            raise InvalidEvent("Initial promise trust outcome must cite exactly its own event.")
        if plan.outcome_event_id in all_refs:
            raise InvalidEvent("Promise trust outcome event was already audited by a bond.")
        _validate_initial_receipt(plan)
        next_refs = refs
        receipt_values[plan.promise_id] = plan.receipt
    elif plan.outcome == "late_return":
        if receipt is None:
            raise InvalidEvent("Late return has no original per-promise breach receipt.")
        receipt = PromiseTrustReceipt.model_validate(receipt.model_dump(mode="python"))
        _validate_late_receipt(plan, receipt, bond, refs, all_refs)
        next_refs = (plan.outcome_event_id,)
        receipt_values[plan.promise_id] = plan.receipt
    else:
        raise InvalidEvent("This promise outcome cannot update the trust projection.")

    prior_refs = bond.evidence_refs if bond is not None else ()
    if any(reference in prior_refs for reference in next_refs):
        raise InvalidEvent("Promise trust plan event has already been applied to this bond.")
    if any(reference in all_refs for reference in next_refs):
        raise InvalidEvent("Promise trust plan event has already been applied to another bond.")

    revision = bond.revision + 1 if bond is not None else 1
    next_bond = PromiseTrustBond(
        id=plan.relation_id,
        world_id=plan.world_id,
        from_id=plan.from_id,
        to_id=plan.to_id,
        revision=revision,
        trust=plan.resulting_trust,
        policy_id=plan.next_budget_policy_id,
        success_budget_spent=plan.next_success_budget_spent,
        evidence_refs=(*prior_refs, *next_refs),
    )
    bond_values = dict(projection.bonds)
    bond_values[next_bond.id] = next_bond
    updated = PromiseTrustProjection(
        world_id=projection.world_id,
        branch_id=expected_branch_id,
        bonds=bond_values,
        receipts=receipt_values,
    )
    verify_promise_trust_projection(
        updated,
        expected_world_id=projection.world_id,
        expected_branch_id=expected_branch_id,
    )
    return updated


def verify_promise_trust_projection(
    projection: PromiseTrustProjection,
    *,
    expected_world_id: UUID,
    expected_branch_id: UUID | None,
) -> None:
    """Verify persisted identity, edge uniqueness and receipt audit provenance."""

    projection = _validated_projection(projection)
    if projection.world_id != expected_world_id or (
        expected_branch_id is not None
        and projection.branch_id is not None
        and projection.branch_id != expected_branch_id
    ):
        raise InvalidEvent("Persisted promise trust projection belongs to another world or branch.")

    bonds: dict[UUID, PromiseTrustBond] = {}
    directed_edges: set[tuple[UUID, UUID]] = set()
    event_to_bond: dict[UUID, UUID] = {}
    for key, raw_bond in projection.bonds.items():
        if not isinstance(key, UUID):
            raise InvalidEvent("Promise trust bond map key must be a UUID.")
        try:
            bond = PromiseTrustBond.model_validate(raw_bond.model_dump(mode="python"))
        except (AttributeError, ValueError) as exc:
            raise InvalidEvent("Persisted promise trust bond is invalid.") from exc
        if key != bond.id:
            raise InvalidEvent("Promise trust bond map key does not match its stable ID.")
        if bond.world_id != projection.world_id:
            raise InvalidEvent("Persisted promise trust bond belongs to another world.")
        edge = (bond.from_id, bond.to_id)
        if edge in directed_edges:
            raise InvalidEvent("Promise trust projection repeats a directed bond endpoint pair.")
        directed_edges.add(edge)
        for reference in bond.evidence_refs:
            if reference in event_to_bond:
                raise InvalidEvent("Promise trust event reference appears more than once across bonds.")
            event_to_bond[reference] = bond.id
        bonds[bond.id] = bond

    receipt_event_refs: set[UUID] = set()
    for promise_id, raw_receipt in projection.receipts.items():
        if not isinstance(promise_id, UUID):
            raise InvalidEvent("Promise trust receipt map key must be a promise UUID.")
        try:
            receipt = PromiseTrustReceipt.model_validate(raw_receipt.model_dump(mode="python"))
        except (AttributeError, ValueError) as exc:
            raise InvalidEvent("Persisted promise trust receipt is invalid.") from exc
        if receipt.promise_id != promise_id:
            raise InvalidEvent("Promise trust receipt map key does not match its promise ID.")
        if receipt.outcome == "on_time_fulfilled" and receipt.applied_delta < 0:
            raise InvalidEvent("On-time fulfillment receipt cannot record a trust loss.")
        if receipt.outcome == "breached" and receipt.applied_delta > 0:
            raise InvalidEvent("Breach receipt must preserve a nonpositive original sanction.")
        primary_bond_id = event_to_bond.get(receipt.outcome_event_id)
        if primary_bond_id is None:
            raise InvalidEvent("Promise trust receipt has no matching bond audit reference.")
        bond = bonds[primary_bond_id]
        if bond.policy_id != receipt.policy_id:
            raise InvalidEvent("Promise trust receipt policy does not match its audited bond.")
        if receipt.outcome_event_id in receipt_event_refs:
            raise InvalidEvent("Promise trust outcome event is referenced by more than one promise receipt.")
        receipt_event_refs.add(receipt.outcome_event_id)

        if receipt.late_return_event_id is not None:
            if receipt.outcome != "breached":
                raise InvalidEvent("Only a breach receipt can include a late-return event.")
            if receipt.late_return_delta is None or receipt.late_return_delta < 0:
                raise InvalidEvent("Late return must retain a nonnegative repair delta.")
            late_bond_id = event_to_bond.get(receipt.late_return_event_id)
            if late_bond_id != primary_bond_id:
                raise InvalidEvent("Late-return receipt event must be audited by the same directed bond.")
            if receipt.late_return_event_id == receipt.outcome_event_id:
                raise InvalidEvent("Breach and late-return audit event IDs must differ.")
            if receipt.late_return_delta > receipt.applied_delta.copy_abs():
                raise InvalidEvent("Late-return repair exceeds the loss recorded by its original breach.")
            if receipt.late_return_event_id in receipt_event_refs:
                raise InvalidEvent("Promise trust late-return event is referenced by more than one receipt.")
            receipt_event_refs.add(receipt.late_return_event_id)

    if receipt_event_refs != set(event_to_bond):
        raise InvalidEvent("Promise trust bond contains audit references without matching promise receipts.")


def _validated_projection(projection: PromiseTrustProjection) -> PromiseTrustProjection:
    if not isinstance(projection, PromiseTrustProjection):
        raise InvalidEvent("Promise trust projection has an invalid type.")
    try:
        world_id = UUID(str(projection.world_id))
        branch_id = UUID(str(projection.branch_id)) if projection.branch_id is not None else None
        bonds = dict(projection.bonds)
        receipts = dict(projection.receipts)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidEvent("Promise trust projection envelope is invalid.") from exc
    return PromiseTrustProjection(
        world_id=world_id,
        branch_id=branch_id,
        bonds=bonds,
        receipts=receipts,
    )


def _validate_plan_cas(plan: PromiseTrustPlan, bond: PromiseTrustBond | None) -> None:
    if bond is None:
        if (
            plan.expected_relation_revision is not None
            or plan.expected_previous_trust is not None
            or plan.expected_budget_policy_id is not None
            or plan.expected_success_budget_spent != 0
        ):
            raise InvalidEvent("Absent promise trust bond does not match plan compare-and-swap values.")
        return
    if (
        bond.id != plan.relation_id
        or bond.world_id != plan.world_id
        or bond.from_id != plan.from_id
        or bond.to_id != plan.to_id
        or bond.revision != plan.expected_relation_revision
        or bond.trust != plan.expected_previous_trust
        or bond.policy_id != plan.expected_budget_policy_id
        or bond.success_budget_spent != plan.expected_success_budget_spent
    ):
        raise InvalidEvent("Promise trust bond compare-and-swap is stale or targets another edge.")


def _validate_plan_values(plan: PromiseTrustPlan, bond: PromiseTrustBond | None) -> None:
    if plan.next_budget_policy_id != plan.policy_id:
        raise InvalidEvent("Promise trust plan next budget policy does not match its effect policy.")
    if bond is not None and bond.policy_id != plan.policy_id:
        raise InvalidEvent("Promise trust plan cannot change a directed bond's authored policy.")
    if plan.expected_budget_policy_id not in {None, plan.policy_id}:
        raise InvalidEvent("Promise trust plan expected budget belongs to another policy.")
    if plan.next_success_budget_spent > _MAX_BUDGET:
        raise InvalidEvent("Promise trust plan success budget exceeds the persisted bound.")
    if plan.outcome == "on_time_fulfilled":
        if plan.next_success_budget_spent < plan.expected_success_budget_spent:
            raise InvalidEvent("On-time fulfillment cannot reduce its cumulative success budget.")
    elif plan.next_success_budget_spent != plan.expected_success_budget_spent:
        raise InvalidEvent("Breach and late return cannot change the cumulative success budget.")

    previous_trust = bond.trust if bond is not None else Decimal("0")
    with localcontext() as context:
        context.prec = 50
        expected_result = max(_MIN_TRUST, min(_MAX_TRUST, previous_trust + plan.applied_delta))
    if plan.resulting_trust != expected_result or plan.applied_delta != plan.resulting_trust - previous_trust:
        raise InvalidEvent("Promise trust plan resulting value does not match its directed contribution delta.")
    if plan.outcome == "on_time_fulfilled" and plan.applied_delta < 0:
        raise InvalidEvent("On-time fulfillment cannot reduce promise trust.")
    if plan.outcome == "breached" and plan.applied_delta > 0:
        raise InvalidEvent("Breach cannot increase promise trust.")
    if plan.outcome == "late_return" and plan.applied_delta < 0:
        raise InvalidEvent("Late return cannot reduce promise trust.")


def _validate_initial_receipt(plan: PromiseTrustPlan) -> None:
    if plan.receipt.outcome != plan.outcome or plan.receipt.outcome_event_id != plan.outcome_event_id:
        raise InvalidEvent("Initial promise trust receipt does not match its outcome event.")
    if plan.receipt.applied_delta != plan.applied_delta:
        raise InvalidEvent("Initial promise trust receipt delta does not match the applied effect.")
    if plan.receipt.late_return_event_id is not None or plan.receipt.late_return_delta is not None:
        raise InvalidEvent("Initial promise trust receipt cannot contain a late-return repair.")


def _validate_late_receipt(
    plan: PromiseTrustPlan,
    prior_receipt: PromiseTrustReceipt,
    bond: PromiseTrustBond | None,
    refs: tuple[UUID, ...],
    all_refs: set[UUID],
) -> None:
    if (
        bond is None
        or prior_receipt.outcome != "breached"
        or prior_receipt.policy_id != plan.policy_id
        or prior_receipt.promise_id != plan.promise_id
        or prior_receipt.late_return_event_id is not None
        or prior_receipt.outcome_event_id not in bond.evidence_refs
        or plan.receipt.outcome != "breached"
        or plan.receipt.outcome_event_id != prior_receipt.outcome_event_id
        or plan.receipt.applied_delta != prior_receipt.applied_delta
        or plan.receipt.assessed_choice != prior_receipt.assessed_choice
        or plan.receipt.late_return_event_id != plan.outcome_event_id
        or plan.receipt.late_return_delta != plan.applied_delta
        or plan.applied_delta > prior_receipt.applied_delta.copy_abs()
        or refs != (prior_receipt.outcome_event_id, plan.outcome_event_id)
    ):
        raise InvalidEvent("Late-return plan does not extend the existing breach sanction receipt.")
    if plan.outcome_event_id in all_refs:
        raise InvalidEvent("Late-return event has already been applied to a trust bond.")

