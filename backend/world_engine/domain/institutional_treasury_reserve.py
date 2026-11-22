"""Pure planning and replay validation for institution reserve mobilization."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from .components import ResourceName, TreasuryComponent
from .errors import InvalidEvent
from .institutional_credit import MAX_INSTITUTIONAL_CREDIT_MINOR, InstitutionalCreditError
from .institutional_credit_treasury import (
    _active_organization,
    _treasury_account,
    _validate_holder_account,
    require_institutional_credit_signer,
)
from .institutional_treasury_reserve_events import (
    INSTITUTIONAL_TREASURY_RESERVE_COMMAND,
    InstitutionalTreasuryMobilizedEvent,
    InstitutionalTreasuryReserveAllocation,
    institutional_treasury_mobilized_command_payload,
    validate_reserve_mobilization_batch,
)
from .models import ResourceAccount, WorldState


_RESOURCE_NAME_ADAPTER = TypeAdapter(ResourceName)


def plan_reserve_mobilization(
    state: WorldState,
    *,
    organization_id: UUID,
    actor_id: UUID,
    currency_resource_id: ResourceName,
    amount_minor: int,
) -> tuple[UUID, tuple[InstitutionalTreasuryReserveAllocation, ...]]:
    """Allocate existing same-currency holder balances to the registered treasury.

    Source accounts are visited in ascending UUID order. Treasury-mapped
    accounts, non-holder accounts, other owners, other resources, and zero
    balances are not available as reserves. Fractional source quantities are
    preserved exactly when they combine to one whole ``amount_minor`` total.
    """
    _validate_amount_and_currency(amount_minor, currency_resource_id)
    require_institutional_credit_signer(state, organization_id, actor_id)
    organization = _active_organization(state, organization_id, "reserve")
    try:
        treasury = TreasuryComponent.model_validate(organization.components.get("treasury"))
    except (TypeError, ValueError) as error:
        raise InstitutionalCreditError(
            "treasury_unavailable", "The organization has no valid registered treasury.",
        ) from error

    destination_account_id, destination = _treasury_account(
        state, organization, currency_resource_id, "treasury",
    )
    _validate_holder_account(destination, organization_id, currency_resource_id, "treasury")
    mapped_treasury_ids = frozenset(treasury.accounts.values())

    sources = [
        (account_id, account)
        for account_id, account in state.resources.items()
        if (
            account_id not in mapped_treasury_ids
            and account.account_type == "holder"
            and account.owner_id == organization_id
            and account.resource_id == currency_resource_id
            and account.credit_limit is None
            and account.balance > 0
        )
    ]
    sources.sort(key=lambda row: row[0])

    remaining = Decimal(amount_minor)
    allocations: list[InstitutionalTreasuryReserveAllocation] = []
    with localcontext() as context:
        context.prec = 80
        for account_id, account in sources:
            if remaining <= 0:
                break
            quantity = min(account.balance, remaining)
            if quantity > 0:
                allocations.append(InstitutionalTreasuryReserveAllocation(
                    account_id=account_id,
                    quantity=quantity,
                ))
                remaining -= quantity

    if remaining != 0:
        raise InstitutionalCreditError(
            "insufficient_reserves",
            "The organization does not have enough same-currency reserve balances.",
        )

    try:
        _resulting_resources(state, destination_account_id, amount_minor, tuple(allocations))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise InstitutionalCreditError(
            "reserve_overflow", "The treasury or reserve accounts cannot represent this transfer exactly.",
        ) from error
    return destination_account_id, tuple(allocations)


def apply_reserve_mobilization(
    state: WorldState,
    event: InstitutionalTreasuryMobilizedEvent,
) -> dict[UUID, ResourceAccount]:
    """Recheck authorization and the canonical plan, then return full resources."""
    if type(event) is not InstitutionalTreasuryMobilizedEvent:
        raise InvalidEvent("Treasury reserve mobilization requires its registered typed event.")
    destination_account_id, allocations = plan_reserve_mobilization(
        state,
        organization_id=event.organization_id,
        actor_id=event.actor_id,
        currency_resource_id=event.currency_resource_id,
        amount_minor=event.amount_minor,
    )
    if (
        destination_account_id != event.destination_account_id
        or allocations != event.allocations
    ):
        raise InvalidEvent("Treasury reserve event does not match the current deterministic allocation plan.")
    try:
        return _resulting_resources(state, destination_account_id, event.amount_minor, allocations)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise InvalidEvent("Treasury reserve balances cannot represent the planned transfer exactly.") from error


def _resulting_resources(
    state: WorldState,
    destination_account_id: UUID,
    amount_minor: int,
    allocations: tuple[InstitutionalTreasuryReserveAllocation, ...],
) -> dict[UUID, ResourceAccount]:
    resources = dict(state.resources)
    destination = resources.get(destination_account_id)
    if destination is None:
        raise ValueError("registered treasury destination is missing")

    with localcontext() as context:
        context.prec = 80
        changes = {
            allocation.account_id: state.resources[allocation.account_id].balance - allocation.quantity
            for allocation in allocations
        }
        destination_balance = destination.balance + Decimal(amount_minor)

    for account_id, balance in changes.items():
        account = state.resources.get(account_id)
        if account is None:
            raise ValueError("reserve source account is missing")
        resources[account_id] = _account_with_balance(account, balance)
    resources[destination_account_id] = _account_with_balance(destination, destination_balance)
    return resources


def _account_with_balance(account: ResourceAccount, balance: Decimal) -> ResourceAccount:
    return ResourceAccount(
        resource_id=account.resource_id,
        balance=balance,
        owner_id=account.owner_id,
        account_type=account.account_type,
        credit_limit=account.credit_limit,
    )


def _validate_amount_and_currency(amount_minor: object, currency_resource_id: object) -> None:
    if (
        type(amount_minor) is not int
        or amount_minor <= 0
        or amount_minor > MAX_INSTITUTIONAL_CREDIT_MINOR
    ):
        raise InstitutionalCreditError(
            "invalid_amount", "Treasury reserve mobilization requires a positive bounded integer amount.",
        )
    if not isinstance(currency_resource_id, str):
        raise InstitutionalCreditError("invalid_currency", "Treasury reserve mobilization requires a currency resource.")
    try:
        _RESOURCE_NAME_ADAPTER.validate_python(currency_resource_id)
    except (TypeError, ValueError, ValidationError) as error:
        raise InstitutionalCreditError(
            "invalid_currency", "Treasury reserve mobilization requires a valid currency resource.",
        ) from error


__all__ = [
    "INSTITUTIONAL_TREASURY_RESERVE_COMMAND",
    "apply_reserve_mobilization",
    "institutional_treasury_mobilized_command_payload",
    "plan_reserve_mobilization",
    "validate_reserve_mobilization_batch",
]

