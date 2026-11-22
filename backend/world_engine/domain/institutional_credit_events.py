"""Command-bound typed event contract for institutional credit lifecycle changes.

The reducer and storage layer additionally bind account ownership and exact
transfer deltas against the current authoritative world state.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, field_validator, model_validator

from .components import ResourceName
from .institutional_credit import (
    MAX_INSTITUTIONAL_CREDIT_MINOR,
    InstitutionalCreditContract,
)
from .json_types import JsonObject


INSTITUTIONAL_CREDIT_CHANGED_EVENT_TYPE = "core.institutional_credit_changed.v1"
_MAX_REVISION = 2**63 - 1

InstitutionalCreditChangedOperation = Literal[
    "offer", "accept", "decline", "withdraw", "pay", "overdue",
]


def institutional_credit_offer_id(command_id: UUID) -> UUID:
    """Derive the new credit identity from its offer command."""
    if not isinstance(command_id, UUID):
        raise ValueError("institutional credit offer identity requires a UUID command ID")
    return uuid5(command_id, "institutional-credit")


def institutional_credit_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: InstitutionalCreditChangedOperation,
    credit_id: UUID,
) -> UUID:
    """Derive one stable event ID from command, ruleset, operation and credit."""
    if not isinstance(command_id, UUID) or not isinstance(credit_id, UUID):
        raise ValueError("institutional credit event identity requires UUID command and credit IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("institutional credit event identity requires a nonempty ruleset version")
    if operation not in {"offer", "accept", "decline", "withdraw", "pay", "overdue"}:
        raise ValueError("institutional credit event operation is unsupported")
    return uuid5(
        command_id,
        f"{ruleset_version}:institutional_credit.{operation}:{credit_id}",
    )


class InstitutionalCreditEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class InstitutionalCreditTransfer(InstitutionalCreditEventModel):
    """Exact integer-minor-unit transfer associated with acceptance or payment."""

    from_account_id: UUID
    to_account_id: UUID
    currency_resource_id: ResourceName
    amount_minor: StrictInt = Field(gt=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR)

    @field_validator("currency_resource_id", mode="before")
    @classmethod
    def strict_currency_identifier(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("currency resource identifier must be text")
        return value

    @model_validator(mode="after")
    def accounts_are_distinct(self) -> InstitutionalCreditTransfer:
        if self.from_account_id == self.to_account_id:
            raise ValueError("institutional credit transfer requires distinct accounts")
        return self


class InstitutionalCreditChangedEvent(InstitutionalCreditEventModel):
    """Private receipt for one institutional credit transition."""

    type: Literal["core.institutional_credit_changed.v1"] = INSTITUTIONAL_CREDIT_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    actor_organization_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    ruleset_version: str = Field(min_length=1, max_length=100)
    operation: InstitutionalCreditChangedOperation
    expected_revision: StrictInt = Field(ge=0, le=_MAX_REVISION)
    resulting_contract: InstitutionalCreditContract
    transfer: InstitutionalCreditTransfer | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def exact_transition_receipt(self) -> InstitutionalCreditChangedEvent:
        contract = self.resulting_contract
        if self.event_id != institutional_credit_event_id(
            self.command_id, self.ruleset_version, self.operation, contract.credit_id,
        ):
            raise ValueError("institutional credit event ID must bind its command, operation, and credit")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("institutional credit lifecycle events are author_only")

        if self.operation == "offer":
            if self.expected_revision != 0 or contract.revision != 1 or contract.status != "offered":
                raise ValueError("an offer must create an offered revision-one contract from revision zero")
            if contract.credit_id != institutional_credit_offer_id(self.command_id):
                raise ValueError("new credit ID must derive from its offer command")
            expected_organization = contract.lender_organization_id
            if self.transfer is not None:
                raise ValueError("an offer cannot transfer funds")
        else:
            if self.expected_revision < 1 or self.expected_revision >= _MAX_REVISION:
                raise ValueError("a credit transition requires an existing revision")
            if contract.revision != self.expected_revision + 1:
                raise ValueError("resulting credit revision must advance exactly one step")
            expected_status = {
                "accept": "active",
                "decline": "declined",
                "withdraw": "withdrawn",
                "overdue": "overdue",
            }.get(self.operation)
            if self.operation == "pay":
                if contract.status not in {"active", "overdue", "settled"}:
                    raise ValueError("payment must leave accepted credit active, overdue, or settled")
            elif contract.status != expected_status:
                raise ValueError("resulting credit status does not match the operation")
            expected_organization = (
                contract.lender_organization_id
                if self.operation in {"withdraw", "overdue"}
                else contract.borrower_organization_id
            )

        if self.operation == "overdue":
            if self.actor_organization_id is not None and self.actor_organization_id != contract.lender_organization_id:
                raise ValueError("an overdue event organization, when supplied, must be the lender")
        elif self.actor_organization_id != expected_organization:
            raise ValueError("institutional credit operation actor organization is not authorized")

        if self.operation == "accept":
            transfer = self._require_transfer()
            if transfer.currency_resource_id != contract.currency_resource_id:
                raise ValueError("acceptance transfer currency must match the credit contract")
            if transfer.amount_minor != contract.principal_minor:
                raise ValueError("acceptance must transfer exactly the contracted principal")
        elif self.operation == "pay":
            transfer = self._require_transfer()
            if transfer.currency_resource_id != contract.currency_resource_id:
                raise ValueError("payment transfer currency must match the credit contract")
            if transfer.amount_minor > contract.repaid_minor:
                raise ValueError("payment transfer cannot exceed resulting cumulative repayment")
            if contract.repaid_minor <= 0:
                raise ValueError("payment transition must increase cumulative repayment")
        elif self.transfer is not None:
            raise ValueError("only acceptance and payment transitions transfer credit funds")

        if self.operation == "overdue":
            if contract.overdue_at_tick is None or contract.status != "overdue":
                raise ValueError("overdue transition must retain the contract breach tick")
        elif self.operation == "pay" and contract.status == "overdue" and contract.overdue_at_tick is None:
            raise ValueError("late payment cannot erase the overdue breach tick")
        return self

    def _require_transfer(self) -> InstitutionalCreditTransfer:
        if self.transfer is None:
            raise ValueError("acceptance and payment transitions require a transfer record")
        return self.transfer


INSTITUTIONAL_CREDIT_CHANGED_EVENT_ADAPTER = TypeAdapter(InstitutionalCreditChangedEvent)


def parse_institutional_credit_changed_event(
    value: InstitutionalCreditChangedEvent | dict[str, object],
) -> InstitutionalCreditChangedEvent:
    """Parse one closed typed institutional credit event."""
    return INSTITUTIONAL_CREDIT_CHANGED_EVENT_ADAPTER.validate_python(value)
