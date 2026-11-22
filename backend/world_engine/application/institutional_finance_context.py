"""Bounded financial facts for the institution's current signatories."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from world_engine.domain.institutional_credit import InstitutionalCreditError
from world_engine.domain.institutional_credit_treasury import require_institutional_credit_signer
from world_engine.domain.institutional_finance import InstitutionalCurrencyProfile, assess_institutional_finance
from world_engine.domain.models import WorldState


_MAX_ORGANIZATIONS = 8
_MAX_CURRENCY_ROWS = 8
_MAX_ASSET_ROWS = 12
_MAX_CREDIT_REFERENCES = 16


def institutional_finance_context(
    state: WorldState, actor_id: UUID, credit: dict[str, Any],
) -> dict[str, Any]:
    """Project only accounts of represented institutions, without account IDs."""
    represented = credit["represented_institutions"]
    organizations = []
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        for institution in represented[:_MAX_ORGANIZATIONS]:
            organization_id = UUID(institution["organization_id"])
            try:
                require_institutional_credit_signer(state, organization_id, actor_id)
            except InstitutionalCreditError:
                continue
            assessment = assess_institutional_finance(state, organization_id)
            reserves: dict[str, Decimal] = defaultdict(Decimal)
            holdings: dict[str, Decimal] = defaultdict(Decimal)
            for asset in assessment.cash_reserve_rows:
                reserves[asset.resource_id] += asset.quantity
            for asset in assessment.asset_rows:
                holdings[asset.resource_id] += asset.quantity
            currencies = sorted(
                assessment.currencies,
                key=lambda row: (-row.due_credit_minor, -row.future_credit_minor, row.currency_resource_id),
            )
            organizations.append({
                "organization_id": str(organization_id),
                "name": institution["name"],
                "currencies": [_currency_projection(row, reserves) for row in currencies[:_MAX_CURRENCY_ROWS]],
                "currencies_truncated": len(currencies) > _MAX_CURRENCY_ROWS,
                "other_assets": [{"resource_id": resource_id, "quantity": format(holdings[resource_id], "f")}
                                 for resource_id in sorted(holdings)[:_MAX_ASSET_ROWS]],
                "other_assets_truncated": len(holdings) > _MAX_ASSET_ROWS,
            })
    return {
        "snapshot_tick": state.current_tick,
        "organizations": organizations,
        "organizations_truncated": credit["represented_institutions_truncated"] or len(represented) > _MAX_ORGANIZATIONS,
    }


def _currency_projection(row: InstitutionalCurrencyProfile, reserves: dict[str, Decimal]) -> dict[str, Any]:
    treasury = row.treasury_balance_minor
    due = row.due_credit_minor
    shortfall = max(Decimal(0), Decimal(due) - max(Decimal(0), treasury)) if treasury is not None else None
    if due == 0:
        status = "no_due_credit"
    elif shortfall is None:
        status = "treasury_unknown"
    elif shortfall:
        status = "cash_shortfall"
    else:
        status = "covered"
    return {
        "currency_resource_id": row.currency_resource_id,
        "treasury_balance_minor": format(treasury, "f") if treasury is not None else None,
        "reserve_cash_minor": format(reserves.get(row.currency_resource_id, Decimal(0)), "f"),
        "due_credit_minor": str(due),
        "overdue_credit_minor": str(row.overdue_credit_minor),
        "future_credit_minor": str(row.future_credit_minor),
        "due_cash_shortfall_minor": format(shortfall, "f") if shortfall is not None else None,
        "liquidity_status": status,
        "due_credit_ids": [str(credit_id) for credit_id in row.due_credit_ids[:_MAX_CREDIT_REFERENCES]],
        "due_credit_ids_truncated": len(row.due_credit_ids) > _MAX_CREDIT_REFERENCES,
    }
