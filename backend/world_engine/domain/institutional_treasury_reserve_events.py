"""Typed, command-bound events for mobilizing existing institution reserves."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, field_validator, model_validator

from .components import ResourceName
from .errors import InvalidEvent
from .institutional_credit import MAX_INSTITUTIONAL_CREDIT_MINOR
from .json_types import JsonObject


INSTITUTIONAL_TREASURY_MOBILIZED_EVENT_TYPE = "core.institutional_treasury_mobilized.v1"
INSTITUTIONAL_TREASURY_RESERVE_COMMAND = "action.mobilize_treasury_reserve"


def institutional_treasury_mobilized_event_id(
    command_id: UUID,
    ruleset_version: str,
    organization_id: UUID,
) -> UUID:
    """Derive a stable reserve-mobilization event ID from its command scope."""
    if not isinstance(command_id, UUID) or not isinstance(organization_id, UUID):
        raise ValueError("institutional treasury event identity requires UUID command and organization IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("institutional treasury event identity requires a nonempty ruleset version")
    return uuid5(
        command_id,
        f"{ruleset_version}:institutional_treasury.mobilize:{organization_id}",
    )


class InstitutionalTreasuryEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class InstitutionalTreasuryReserveAllocation(InstitutionalTreasuryEventModel):
    """One exact positive reserve-account quantity allocated to a treasury."""

    account_id: UUID
    quantity: Decimal = Field(gt=Decimal("0"))

    @field_validator("quantity", mode="before")
    @classmethod
    def parse_exact_quantity(cls, value: object) -> Decimal:
        if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("reserve allocations must use decimal strings or integers, not binary floats")
        try:
            quantity = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("reserve allocation quantity is not a valid decimal") from error
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("reserve allocation quantity must be finite and positive")
        scale, integer_digits = _decimal_shape(quantity)
        if scale > 6 or integer_digits > 30:
            raise ValueError("reserve allocation quantity exceeds supported precision")
        return quantity


class InstitutionalTreasuryMobilizedEvent(InstitutionalTreasuryEventModel):
    """Private receipt for moving exact existing holder balances to a treasury."""

    type: Literal[
        "core.institutional_treasury_mobilized.v1"
    ] = INSTITUTIONAL_TREASURY_MOBILIZED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    organization_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    currency_resource_id: ResourceName
    amount_minor: StrictInt = Field(gt=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR)
    destination_account_id: UUID
    allocations: tuple[InstitutionalTreasuryReserveAllocation, ...] = Field(min_length=1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @field_validator("currency_resource_id", mode="before")
    @classmethod
    def strict_currency_identifier(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("currency resource identifier must be text")
        return value

    @model_validator(mode="after")
    def exact_mobilization_receipt(self) -> InstitutionalTreasuryMobilizedEvent:
        if self.event_id != institutional_treasury_mobilized_event_id(
            self.command_id, self.ruleset_version, self.organization_id,
        ):
            raise ValueError("institutional treasury event ID must bind its command and organization")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("institutional treasury mobilization events are author_only")
        account_ids = [row.account_id for row in self.allocations]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("reserve allocations must use unique source account IDs")
        if self.destination_account_id in account_ids:
            raise ValueError("reserve allocation sources must be distinct from the treasury destination")
        with localcontext() as context:
            context.prec = 80
            allocated_total = sum((row.quantity for row in self.allocations), Decimal("0"))
        if allocated_total != Decimal(self.amount_minor):
            raise ValueError("reserve allocation quantities must exactly equal the mobilized amount")
        return self


INSTITUTIONAL_TREASURY_MOBILIZED_EVENT_ADAPTER = TypeAdapter(InstitutionalTreasuryMobilizedEvent)


def parse_institutional_treasury_mobilized_event(
    value: InstitutionalTreasuryMobilizedEvent | dict[str, object],
) -> InstitutionalTreasuryMobilizedEvent:
    """Parse a closed typed treasury-reserve event."""
    return INSTITUTIONAL_TREASURY_MOBILIZED_EVENT_ADAPTER.validate_python(value)


def institutional_treasury_mobilized_command_payload(
    event: InstitutionalTreasuryMobilizedEvent,
) -> dict[str, object]:
    """Return the minimal user-authored request bound to this event."""
    return {
        "kind": "mobilize_treasury_reserve",
        "organization_id": str(event.organization_id),
        "currency_resource_id": event.currency_resource_id,
        "amount_minor": event.amount_minor,
    }


def validate_reserve_mobilization_batch(
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    """Bind the sole reserve event to its scoped command or archived context."""
    event = next((item for item in events if isinstance(item, InstitutionalTreasuryMobilizedEvent)), None)
    if event is None:
        if command_type == INSTITUTIONAL_TREASURY_RESERVE_COMMAND:
            raise InvalidEvent("treasury reserve commands require their typed mobilization event")
        return
    if len(events) != 1 or (
        event.world_id,
        event.branch_id,
        event.command_id,
        event.actor_id,
        event.ruleset_version,
    ) != (world_id, branch_id, command_id, actor_id, ruleset_version):
        raise InvalidEvent("treasury reserve event must match a single scoped command")
    if command_type is None:
        return
    if (
        command_type != INSTITUTIONAL_TREASURY_RESERVE_COMMAND
        or command_payload != institutional_treasury_mobilized_command_payload(event)
    ):
        raise InvalidEvent("treasury reserve event does not match its command proposal")


def _decimal_shape(value: Decimal) -> tuple[int, int]:
    """Match exact resource-account precision checks without context changes."""
    digits = list(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    scale = max(0, -exponent)
    integer_digits = max(1, len(digits) + exponent)
    return scale, integer_digits

