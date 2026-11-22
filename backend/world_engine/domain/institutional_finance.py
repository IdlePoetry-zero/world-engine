"""Deterministic institution finance facts derived from the current world state.

This projection reports exact registered treasury balances, outstanding
borrower-side term credit, and owned holder accounts. It does not estimate
asset values, authorize a decision, or infer insolvency.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from .components import TreasuryComponent
from .models import ResourceAccount, WorldState


_ORGANIZATION_KINDS = frozenset({"organization", "polity", "faction", "company", "government"})


@dataclass(frozen=True, slots=True)
class InstitutionalAssetRow:
    """An owned positive holder balance with no valuation conversion."""

    account_id: UUID
    resource_id: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class InstitutionalCurrencyProfile:
    """Exact treasury and borrower-side credit facts for one resource currency."""

    currency_resource_id: str
    treasury_balance_minor: Decimal | None
    due_credit_minor: int
    overdue_credit_minor: int
    future_credit_minor: int
    due_credit_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class InstitutionalFinanceAssessment:
    """Sorted read-only finance facts for one institution."""

    currencies: tuple[InstitutionalCurrencyProfile, ...]
    asset_rows: tuple[InstitutionalAssetRow, ...]
    cash_reserve_rows: tuple[InstitutionalAssetRow, ...]


def assess_institutional_finance(
    state: WorldState,
    organization_id: UUID,
) -> InstitutionalFinanceAssessment:
    """Return deterministic balances, credit obligations, and owned assets.

    Credit totals describe funded borrower obligations only. They use each
    contract's exact remaining total in integer minor units. The treasury field
    is the account's exact ``Decimal`` quantity; ``None`` means no valid
    registered treasury account could be verified, while ``Decimal("0")`` is
    a known zero balance.

    Asset rows include positive holder accounts owned by the organization,
    except mapped treasury accounts and all resource IDs registered as its
    treasury currencies. Other holders of those registered currencies are
    returned separately as cash reserves.
    """

    organization = state.entities.get(organization_id)
    if organization is None or organization.kind not in _ORGANIZATION_KINDS:
        raise ValueError("institutional finance requires an existing organization entity")

    try:
        treasury = TreasuryComponent.model_validate(organization.components.get("treasury"))
    except (TypeError, ValueError):
        treasury_accounts: dict[str, UUID] = {}
    else:
        treasury_accounts = dict(treasury.accounts)

    registered_currency_ids = frozenset(treasury_accounts)
    mapped_treasury_account_ids = frozenset(treasury_accounts.values())

    treasury_balances: dict[str, Decimal | None] = {}
    for currency_resource_id, account_id in treasury_accounts.items():
        account = state.resources.get(account_id)
        treasury_balances[currency_resource_id] = (
            account.balance
            if _is_valid_treasury_account(account, organization_id, currency_resource_id)
            else None
        )

    asset_rows: list[InstitutionalAssetRow] = []
    cash_reserve_rows: list[InstitutionalAssetRow] = []
    for account_id, account in state.resources.items():
        if (
            account.account_type != "holder"
            or account.owner_id != organization_id
            or account.balance <= 0
        ):
            continue
        row = InstitutionalAssetRow(
            account_id=account_id,
            resource_id=account.resource_id,
            quantity=account.balance,
        )
        if account_id in mapped_treasury_account_ids:
            continue
        if account.resource_id in registered_currency_ids:
            cash_reserve_rows.append(row)
        else:
            asset_rows.append(row)

    due_by_currency: dict[str, int] = {}
    overdue_by_currency: dict[str, int] = {}
    future_by_currency: dict[str, int] = {}
    due_ids_by_currency: dict[str, list[UUID]] = {}
    currencies = set(treasury_accounts)

    for contract in state.institutional_credit_contracts:
        if (
            contract.borrower_organization_id != organization_id
            or contract.status not in {"active", "overdue"}
        ):
            continue
        currency_resource_id = contract.currency_resource_id
        currencies.add(currency_resource_id)
        remaining = contract.remaining_total_minor
        if contract.status == "overdue":
            overdue_by_currency[currency_resource_id] = (
                overdue_by_currency.get(currency_resource_id, 0) + remaining
            )
        if contract.maturity_tick <= state.current_tick:
            due_by_currency[currency_resource_id] = (
                due_by_currency.get(currency_resource_id, 0) + remaining
            )
            due_ids_by_currency.setdefault(currency_resource_id, []).append(contract.credit_id)
        elif contract.status == "active":
            future_by_currency[currency_resource_id] = (
                future_by_currency.get(currency_resource_id, 0) + remaining
            )

    profiles = tuple(
        InstitutionalCurrencyProfile(
            currency_resource_id=currency_resource_id,
            treasury_balance_minor=treasury_balances.get(currency_resource_id),
            due_credit_minor=due_by_currency.get(currency_resource_id, 0),
            overdue_credit_minor=overdue_by_currency.get(currency_resource_id, 0),
            future_credit_minor=future_by_currency.get(currency_resource_id, 0),
            due_credit_ids=tuple(sorted(due_ids_by_currency.get(currency_resource_id, ()))),
        )
        for currency_resource_id in sorted(currencies)
    )

    return InstitutionalFinanceAssessment(
        currencies=profiles,
        asset_rows=tuple(sorted(asset_rows, key=_asset_sort_key)),
        cash_reserve_rows=tuple(sorted(cash_reserve_rows, key=_asset_sort_key)),
    )


def _is_valid_treasury_account(
    account: ResourceAccount | None,
    organization_id: UUID,
    currency_resource_id: str,
) -> bool:
    return bool(
        account is not None
        and account.account_type == "holder"
        and account.owner_id == organization_id
        and account.resource_id == currency_resource_id
        and account.credit_limit is None
    )


def _asset_sort_key(row: InstitutionalAssetRow) -> tuple[str, str]:
    return row.resource_id, row.account_id.hex

