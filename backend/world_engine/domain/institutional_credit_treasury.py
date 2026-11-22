"""Authorized, exact treasury transfers for institutional term credit.

This module plans immutable account movements only. Callers still own the
contract transition and must commit the returned account updates with the
corresponding typed credit event in one repository transaction.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError

from .components import (
    GovernanceComponent, OfficeRecord,
    ResourceName,
    TreasuryComponent,
    is_leadership_office,
)
from .institutional_credit import (
    MAX_INSTITUTIONAL_CREDIT_MINOR,
    InstitutionalCreditError,
)
from .models import ResourceAccount, WorldEntity, WorldState

if TYPE_CHECKING:
    from .institutional_credit_events import InstitutionalCreditTransfer


_ORGANIZATION_KINDS = frozenset({"organization", "polity", "faction", "company", "government"})
_PERSON_KINDS = frozenset({"character", "person"})


def require_institutional_credit_signer(
    state: WorldState,
    organization_id: UUID,
    actor_id: UUID,
) -> None:
    """Require a live member with current representation or treasury authority."""

    organization = state.entities.get(organization_id)
    actor = state.entities.get(actor_id)
    if (
        organization is None
        or organization.kind not in _ORGANIZATION_KINDS
        or organization.status != "active"
    ):
        raise InstitutionalCreditError("organization_unavailable", "Institutional credit requires an active organization.")
    if (
        actor is None
        or actor.kind not in _PERSON_KINDS
        or actor.status != "active"
    ):
        raise InstitutionalCreditError("signer_unavailable", "Institutional credit requires an active character signer.")
    try:
        governance = GovernanceComponent.model_validate(organization.components.get("governance"))
    except (TypeError, ValueError) as error:
        raise InstitutionalCreditError("signer_unauthorized", "The organization has no valid signer authority record.") from error
    if actor_id not in governance.members:
        raise InstitutionalCreditError("signer_unauthorized", "The signer is not an active organization member.")

    current_tick = state.current_tick
    if type(current_tick) is not int:
        raise InstitutionalCreditError("signer_unauthorized", "The current organization authority tick is invalid.")

    def office_is_live(office: OfficeRecord) -> bool:
        return office.term_end_tick is None or current_tick < office.term_end_tick

    holds_treasury_authority = any(
        office.holder_id == actor_id
        and "authority.treasury" in office.authority
        and office_is_live(office)
        for office in governance.offices
    )
    is_authorized_representative = (
        governance.leader_id == actor_id
        and any(
            office.holder_id == actor_id
            and is_leadership_office(office)
            and "authority.represent" in office.authority
            and office_is_live(office)
            for office in governance.offices
        )
    )
    if not (holds_treasury_authority or is_authorized_representative):
        raise InstitutionalCreditError(
            "signer_unauthorized",
            "The signer does not hold a current representative or treasury-authority office.",
        )


def plan_institutional_credit_transfer(
    state: WorldState,
    *,
    debit_organization_id: UUID,
    credit_organization_id: UUID,
    currency_resource_id: ResourceName,
    amount_minor: int,
) -> InstitutionalCreditTransfer:
    """Plan a finite same-currency transfer between registered organization treasuries."""

    if debit_organization_id == credit_organization_id:
        raise InstitutionalCreditError("invalid_transfer", "Institutional credit requires two distinct organizations.")
    _validate_amount_and_resource(amount_minor, currency_resource_id)
    debit_organization = _active_organization(state, debit_organization_id, "debit")
    credit_organization = _active_organization(state, credit_organization_id, "credit")
    debit_account_id, debit_account = _treasury_account(
        state, debit_organization, currency_resource_id, "debit",
    )
    credit_account_id, credit_account = _treasury_account(
        state, credit_organization, currency_resource_id, "credit",
    )
    if debit_account_id == credit_account_id:
        raise InstitutionalCreditError("invalid_transfer", "Institutional credit treasuries must use distinct accounts.")
    _validate_holder_account(debit_account, debit_organization_id, currency_resource_id, "debit")
    _validate_holder_account(credit_account, credit_organization_id, currency_resource_id, "credit")
    if debit_account.balance < Decimal(amount_minor):
        raise InstitutionalCreditError("insufficient_funds", "The lending treasury does not have enough available funds.")

    from .institutional_credit_events import InstitutionalCreditTransfer

    try:
        transfer = InstitutionalCreditTransfer(
            from_account_id=debit_account_id,
            to_account_id=credit_account_id,
            currency_resource_id=currency_resource_id,
            amount_minor=amount_minor,
        )
    except (TypeError, ValueError) as error:
        raise InstitutionalCreditError("invalid_transfer", "Institutional credit transfer terms are invalid.") from error
    # Validate both resulting account values now so overflow or scale errors are
    # rejected before a caller builds a contract event around this plan.
    _updated_accounts(state, transfer)
    return transfer


def apply_institutional_credit_transfer(
    state: WorldState,
    transfer: InstitutionalCreditTransfer,
) -> dict[UUID, ResourceAccount]:
    """Return exact validated account replacements for the current world state.

    The function does not mutate ``state`` and must be called against the state
    that the caller will commit. It verifies current treasury mappings again,
    so a stale plan cannot redirect funds to a different organization account.
    """

    from .institutional_credit_events import InstitutionalCreditTransfer

    if type(transfer) is not InstitutionalCreditTransfer:
        raise InstitutionalCreditError("invalid_transfer", "The institutional credit transfer is not a registered plan.")
    _validate_amount_and_resource(transfer.amount_minor, transfer.currency_resource_id)
    from_account = state.resources.get(transfer.from_account_id)
    to_account = state.resources.get(transfer.to_account_id)
    if transfer.from_account_id == transfer.to_account_id or from_account is None or to_account is None:
        raise InstitutionalCreditError("treasury_unavailable", "The planned treasury accounts are unavailable.")

    debit_owner_id = from_account.owner_id
    credit_owner_id = to_account.owner_id
    if debit_owner_id is None or credit_owner_id is None or debit_owner_id == credit_owner_id:
        raise InstitutionalCreditError("treasury_unavailable", "The planned treasury account owners are invalid.")
    debit_organization = _active_organization(state, debit_owner_id, "debit")
    credit_organization = _active_organization(state, credit_owner_id, "credit")
    expected_from_id, expected_from = _treasury_account(
        state, debit_organization, transfer.currency_resource_id, "debit",
    )
    expected_to_id, expected_to = _treasury_account(
        state, credit_organization, transfer.currency_resource_id, "credit",
    )
    if (
        expected_from_id != transfer.from_account_id
        or expected_to_id != transfer.to_account_id
        or expected_from != from_account
        or expected_to != to_account
    ):
        raise InstitutionalCreditError("treasury_unavailable", "The planned treasury accounts no longer match the registered mapping.")
    _validate_holder_account(from_account, debit_owner_id, transfer.currency_resource_id, "debit")
    _validate_holder_account(to_account, credit_owner_id, transfer.currency_resource_id, "credit")
    if from_account.balance < Decimal(transfer.amount_minor):
        raise InstitutionalCreditError("insufficient_funds", "The lending treasury no longer has enough available funds.")
    return _updated_accounts(state, transfer)


def _updated_accounts(
    state: WorldState,
    transfer: InstitutionalCreditTransfer,
) -> dict[UUID, ResourceAccount]:
    debit = state.resources.get(transfer.from_account_id)
    credit = state.resources.get(transfer.to_account_id)
    if debit is None or credit is None or transfer.from_account_id == transfer.to_account_id:
        raise InstitutionalCreditError("treasury_unavailable", "The planned treasury accounts are unavailable.")
    amount = Decimal(transfer.amount_minor)
    if debit.balance < amount:
        raise InstitutionalCreditError("insufficient_funds", "The lending treasury does not have enough available funds.")
    try:
        with localcontext() as context:
            context.prec = 80
            next_debit = debit.balance - amount
            next_credit = credit.balance + amount
        debit_update = ResourceAccount(
            resource_id=debit.resource_id,
            balance=next_debit,
            owner_id=debit.owner_id,
            account_type="holder",
        )
        credit_update = ResourceAccount(
            resource_id=credit.resource_id,
            balance=next_credit,
            owner_id=credit.owner_id,
            account_type="holder",
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise InstitutionalCreditError("destination_overflow", "The receiving treasury cannot hold this amount.") from error
    return {
        transfer.from_account_id: debit_update,
        transfer.to_account_id: credit_update,
    }


def _validate_amount_and_resource(amount_minor: object, currency_resource_id: object) -> None:
    if (
        type(amount_minor) is not int
        or amount_minor <= 0
        or amount_minor > MAX_INSTITUTIONAL_CREDIT_MINOR
    ):
        raise InstitutionalCreditError("invalid_amount", "Institutional credit transfers require a positive bounded integer amount.")
    if not isinstance(currency_resource_id, str) or not currency_resource_id.strip():
        raise InstitutionalCreditError("invalid_currency", "Institutional credit requires one registered currency resource.")


def _active_organization(state: WorldState, organization_id: UUID, label: str) -> WorldEntity:
    organization = state.entities.get(organization_id)
    if (
        organization is None
        or organization.kind not in _ORGANIZATION_KINDS
        or organization.status != "active"
    ):
        raise InstitutionalCreditError("organization_unavailable", f"The {label} organization is unavailable.")
    return organization


def _treasury_account(
    state: WorldState,
    organization: WorldEntity,
    currency_resource_id: str,
    label: str,
) -> tuple[UUID, ResourceAccount]:
    try:
        treasury = TreasuryComponent.model_validate(organization.components.get("treasury"))
    except (TypeError, ValueError) as error:
        raise InstitutionalCreditError("treasury_unavailable", f"The {label} organization has no registered treasury.") from error
    account_id = treasury.accounts.get(currency_resource_id)
    account = state.resources.get(account_id) if account_id is not None else None
    if account_id is None or account is None:
        raise InstitutionalCreditError("treasury_unavailable", f"The {label} organization has no account for this currency.")
    return account_id, account


def _validate_holder_account(
    account: ResourceAccount,
    organization_id: UUID,
    currency_resource_id: str,
    label: str,
) -> None:
    if (
        account.account_type != "holder"
        or account.owner_id != organization_id
        or account.resource_id != currency_resource_id
        or account.credit_limit is not None
    ):
        raise InstitutionalCreditError(
            "treasury_unavailable",
            f"The {label} treasury account does not match its organization and currency.",
        )
