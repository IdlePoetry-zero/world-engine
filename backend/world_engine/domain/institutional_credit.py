"""Pure, immutable lifecycle for one institutional term-credit agreement.

This module defines the agreement and its valid transitions only. Authorization,
treasury accounts, ledger settlement, event persistence, and scheduling belong to
the application and storage layers that integrate this contract.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .components import ResourceName
from .institutional_credit_extension import (
    InstitutionalCreditExtension,
    validate_credit_extension_history,
)


MAX_INSTITUTIONAL_CREDIT_MINOR = 10**30 - 1
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
_MAX_REVISION = 2**63 - 1

InstitutionalCreditStatus = Literal[
    "offered", "active", "declined", "withdrawn", "overdue", "settled",
]


class InstitutionalCreditModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class InstitutionalCreditContract(InstitutionalCreditModel):
    """A fixed-principal, fixed-total-repayment credit agreement.

    Monetary amounts use exact integer minor units. An omitted total repayment
    means principal only; this model does not accrue interest or fees over time.
    """

    credit_id: UUID
    lender_organization_id: UUID
    borrower_organization_id: UUID
    currency_resource_id: ResourceName
    principal_minor: StrictInt = Field(gt=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR)
    total_repayable_minor: StrictInt | None = Field(
        default=None,
        ge=1,
        le=MAX_INSTITUTIONAL_CREDIT_MINOR,
        exclude_if=lambda value: value is None,
    )
    accepted_through_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    maturity_tick: StrictInt = Field(ge=_MIN_TICK, lt=_MAX_TICK)
    created_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    updated_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    revision: StrictInt = Field(default=1, ge=1, le=_MAX_REVISION)
    status: InstitutionalCreditStatus = "offered"
    accepted_tick: StrictInt | None = Field(
        default=None, ge=_MIN_TICK, le=_MAX_TICK, exclude_if=lambda value: value is None,
    )
    repaid_minor: StrictInt = Field(default=0, ge=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR)
    overdue_at_tick: StrictInt | None = Field(
        default=None, ge=_MIN_TICK, le=_MAX_TICK, exclude_if=lambda value: value is None,
    )
    extensions: tuple[InstitutionalCreditExtension, ...] = Field(
        default_factory=tuple,
        exclude_if=lambda value: not value,
    )

    @field_validator("currency_resource_id", mode="before")
    @classmethod
    def strict_currency_identifier(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("currency resource identifier must be text")
        return value

    @model_validator(mode="after")
    def valid_parties_and_lifecycle(self) -> InstitutionalCreditContract:
        if self.lender_organization_id == self.borrower_organization_id:
            raise ValueError("institutional credit requires distinct lender and borrower organizations")
        if self.accepted_through_tick >= self.maturity_tick:
            raise ValueError("acceptance deadline must precede maturity")
        if self.created_tick > self.updated_tick:
            raise ValueError("updated tick cannot precede offer creation")
        if self.created_tick > self.accepted_through_tick:
            raise ValueError("offer creation cannot follow its acceptance deadline")

        total = self.total_due_minor
        if self.total_repayable_minor is not None and self.total_repayable_minor < self.principal_minor:
            raise ValueError("total repayable must be at least principal")
        if self.repaid_minor > total:
            raise ValueError("repaid amount cannot exceed total due")

        if self.status in {"offered", "declined", "withdrawn"}:
            if self.accepted_tick is not None or self.repaid_minor != 0 or self.overdue_at_tick is not None:
                raise ValueError("unaccepted credit cannot contain acceptance, repayment, or overdue facts")
        else:
            if self.accepted_tick is None:
                raise ValueError("accepted credit requires its acceptance tick")
            if not self.created_tick <= self.accepted_tick <= self.accepted_through_tick:
                raise ValueError("acceptance tick must fall within the offer window")
            if self.updated_tick < self.accepted_tick:
                raise ValueError("credit update tick cannot precede acceptance")

        if self.status == "overdue":
            if self.overdue_at_tick is None or self.overdue_at_tick <= self.maturity_tick:
                raise ValueError("overdue credit requires a tick after inclusive maturity")
            if self.updated_tick < self.overdue_at_tick or self.repaid_minor >= total:
                raise ValueError("overdue credit must retain a positive balance at or after its breach tick")
        elif self.status == "settled":
            if self.repaid_minor != total:
                raise ValueError("settled credit must have its full total repaid")
            if self.overdue_at_tick is not None:
                if self.overdue_at_tick <= self.maturity_tick or self.updated_tick < self.overdue_at_tick:
                    raise ValueError("settlement must preserve an overdue breach after maturity")
            elif self.updated_tick > self.maturity_tick:
                raise ValueError("late settlement must preserve the overdue breach tick")
        elif self.overdue_at_tick is not None:
            raise ValueError("only overdue or settled credit may retain an overdue breach tick")
        elif self.status == "active":
            if self.repaid_minor >= total:
                raise ValueError("fully repaid credit must be settled")
            if self.updated_tick > self.maturity_tick:
                raise ValueError("active credit cannot move past maturity without overdue status")
        validate_credit_extension_history(
            self.extensions,
            maturity_tick=self.maturity_tick,
            status=self.status,
            accepted_tick=self.accepted_tick,
            accepted_through_tick=self.accepted_through_tick,
            updated_tick=self.updated_tick,
            lender_organization_id=self.lender_organization_id,
            borrower_organization_id=self.borrower_organization_id,
        )
        return self

    @property
    def total_due_minor(self) -> int:
        return self.principal_minor if self.total_repayable_minor is None else self.total_repayable_minor

    @property
    def remaining_total_minor(self) -> int:
        return self.total_due_minor - self.repaid_minor

    @property
    def remaining_principal_minor(self) -> int:
        """Principal is paid first; additional repayment covers the fixed excess."""
        return max(self.principal_minor - self.repaid_minor, 0)


class InstitutionalCreditError(ValueError):
    """A requested credit transition does not match the agreement or current tick."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def propose_institutional_credit(
    *,
    credit_id: UUID,
    lender_organization_id: UUID,
    borrower_organization_id: UUID,
    currency_resource_id: str,
    principal_minor: int,
    accepted_through_tick: int,
    maturity_tick: int,
    current_tick: int,
    total_repayable_minor: int | None = None,
) -> InstitutionalCreditContract:
    """Create a lender-signed offer with revision one at ``current_tick``."""
    _require_tick(current_tick, "current tick")
    try:
        return InstitutionalCreditContract(
            credit_id=credit_id,
            lender_organization_id=lender_organization_id,
            borrower_organization_id=borrower_organization_id,
            currency_resource_id=currency_resource_id,
            principal_minor=principal_minor,
            total_repayable_minor=total_repayable_minor,
            accepted_through_tick=accepted_through_tick,
            maturity_tick=maturity_tick,
            created_tick=current_tick,
            updated_tick=current_tick,
            revision=1,
            status="offered",
        )
    except (TypeError, ValueError) as error:
        raise InstitutionalCreditError("invalid_terms", "Institutional credit terms are invalid.") from error


def accept_institutional_credit(
    contract: InstitutionalCreditContract,
    *,
    actor_organization_id: UUID,
    current_tick: int,
    expected_revision: int,
) -> InstitutionalCreditContract:
    """Accept an offered credit using the borrower organization identity."""
    _require_transition(contract, actor_organization_id, current_tick, expected_revision)
    _require_party(actor_organization_id, contract.borrower_organization_id)
    _require_status(contract, "offered")
    if current_tick > contract.accepted_through_tick:
        raise InstitutionalCreditError("acceptance_window_closed", "The offer can no longer be accepted.")
    if current_tick >= contract.maturity_tick:
        raise InstitutionalCreditError("maturity_reached", "The offer cannot be accepted at or after maturity.")
    return _advance(contract, current_tick, status="active", accepted_tick=current_tick)


def decline_institutional_credit(
    contract: InstitutionalCreditContract,
    *,
    actor_organization_id: UUID,
    current_tick: int,
    expected_revision: int,
) -> InstitutionalCreditContract:
    """Decline an offer as its named borrower organization."""
    _require_transition(contract, actor_organization_id, current_tick, expected_revision)
    _require_party(actor_organization_id, contract.borrower_organization_id)
    _require_status(contract, "offered")
    return _advance(contract, current_tick, status="declined")


def withdraw_institutional_credit(
    contract: InstitutionalCreditContract,
    *,
    actor_organization_id: UUID,
    current_tick: int,
    expected_revision: int,
) -> InstitutionalCreditContract:
    """Withdraw an outstanding offer as its named lender organization."""
    _require_transition(contract, actor_organization_id, current_tick, expected_revision)
    _require_party(actor_organization_id, contract.lender_organization_id)
    _require_status(contract, "offered")
    return _advance(contract, current_tick, status="withdrawn")


def pay_institutional_credit(
    contract: InstitutionalCreditContract,
    *,
    actor_organization_id: UUID,
    amount_minor: int,
    current_tick: int,
    expected_revision: int,
) -> InstitutionalCreditContract:
    """Record a bounded partial or exact payment by the borrower organization."""
    _require_transition(contract, actor_organization_id, current_tick, expected_revision)
    _require_party(actor_organization_id, contract.borrower_organization_id)
    _require_positive_minor(amount_minor, "payment amount")
    if amount_minor > contract.remaining_total_minor:
        raise InstitutionalCreditError("overpayment", "Payment cannot exceed the outstanding total.")
    if contract.status == "active":
        if current_tick > contract.maturity_tick:
            raise InstitutionalCreditError("overdue_not_marked", "Overdue status must be recorded before a late payment.")
    elif contract.status == "overdue":
        if contract.overdue_at_tick is None or current_tick < contract.overdue_at_tick:
            raise InstitutionalCreditError("invalid_overdue_tick", "Payment tick precedes the recorded overdue state.")
    else:
        raise InstitutionalCreditError("invalid_status", "Only active or overdue credit can receive payments.")

    repaid = contract.repaid_minor + amount_minor
    if repaid == contract.total_due_minor:
        return _advance(contract, current_tick, status="settled", repaid_minor=repaid)
    return _advance(contract, current_tick, repaid_minor=repaid)


def mark_institutional_credit_overdue(
    contract: InstitutionalCreditContract,
    *,
    actor_organization_id: UUID,
    current_tick: int,
    expected_revision: int,
) -> InstitutionalCreditContract:
    """Record breach after the inclusive maturity tick while a balance remains."""
    _require_transition(contract, actor_organization_id, current_tick, expected_revision)
    _require_party(actor_organization_id, contract.lender_organization_id)
    _require_status(contract, "active")
    if current_tick <= contract.maturity_tick:
        raise InstitutionalCreditError("not_overdue", "Credit is not overdue before the tick after maturity.")
    if contract.remaining_total_minor <= 0:
        raise InstitutionalCreditError("no_balance_due", "Fully repaid credit cannot become overdue.")
    return _advance(contract, current_tick, status="overdue", overdue_at_tick=current_tick)


def _advance(
    contract: InstitutionalCreditContract,
    current_tick: int,
    **changes: object,
) -> InstitutionalCreditContract:
    if contract.revision >= _MAX_REVISION:
        raise InstitutionalCreditError("revision_exhausted", "Credit revision cannot be advanced further.")
    raw = contract.model_dump(mode="python") | {
        "revision": contract.revision + 1,
        "updated_tick": current_tick,
        **changes,
    }
    try:
        return InstitutionalCreditContract.model_validate(raw)
    except (TypeError, ValueError) as error:
        raise InstitutionalCreditError("invalid_transition", "The requested credit transition is invalid.") from error


def _require_transition(
    contract: InstitutionalCreditContract,
    actor_organization_id: UUID,
    current_tick: int,
    expected_revision: int,
) -> None:
    if not isinstance(contract, InstitutionalCreditContract):
        raise InstitutionalCreditError("invalid_contract", "A typed institutional credit contract is required.")
    if not isinstance(actor_organization_id, UUID):
        raise InstitutionalCreditError("invalid_actor", "A known organization identity is required.")
    _require_tick(current_tick, "current tick")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or not 1 <= expected_revision <= _MAX_REVISION:
        raise InstitutionalCreditError("invalid_revision", "Expected revision must be a positive integer.")
    if contract.revision != expected_revision:
        raise InstitutionalCreditError("stale_revision", "Credit revision does not match the expected revision.")
    if current_tick < contract.updated_tick:
        raise InstitutionalCreditError("time_reversed", "Credit transitions cannot move backward in world time.")


def _require_tick(tick: int, label: str) -> None:
    if not isinstance(tick, int) or isinstance(tick, bool) or not _MIN_TICK <= tick <= _MAX_TICK:
        raise InstitutionalCreditError("invalid_tick", f"{label.capitalize()} must be a signed 64-bit integer.")


def _require_positive_minor(amount: int, label: str) -> None:
    if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= MAX_INSTITUTIONAL_CREDIT_MINOR:
        raise InstitutionalCreditError("invalid_amount", f"{label.capitalize()} must be a positive bounded integer.")


def _require_party(actor_id: UUID, expected_id: UUID) -> None:
    if actor_id != expected_id:
        raise InstitutionalCreditError("wrong_organization", "This organization is not a party authorized for the transition.")


def _require_status(contract: InstitutionalCreditContract, expected: InstitutionalCreditStatus) -> None:
    if contract.status != expected:
        raise InstitutionalCreditError("invalid_status", f"Transition requires credit status {expected!r}.")
