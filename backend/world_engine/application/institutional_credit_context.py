"""Bounded signer-safe facts for institutional term credit.

This projection contains no treasury balances or account identifiers. It only
shows institutions the actor can currently sign for, visible organizations
with a shared registered treasury resource, and contracts tied to an
institution the actor can currently sign for. Funding and all command
preconditions are checked again by the authoritative command path.
"""

from __future__ import annotations

from heapq import nsmallest
from typing import Any, Callable
from uuid import UUID

from pydantic import ValidationError

from world_engine.domain.components import TreasuryComponent
from world_engine.domain.institutional_credit import InstitutionalCreditContract
from world_engine.domain.institutional_credit_treasury import (
    require_institutional_credit_signer,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState


MAX_CONTEXT_ROWS = 32
MAX_CURRENCIES_PER_ROW = 16
MAX_EXTENSIONS_PER_CONTRACT = 8
_ORGANIZATION_KINDS = frozenset({"organization", "polity", "faction", "company", "government"})
_PERSON_KINDS = frozenset({"character", "person"})
_PHASES = {
    "offered": "offer_pending",
    "active": "repayment_open",
    "declined": "declined",
    "withdrawn": "withdrawn",
    "overdue": "overdue",
    "settled": "settled",
}
_CONTRACT_ATTENTION_ORDER = {"overdue": 0, "repayment_open": 1, "offer_pending": 2}


def institutional_credit_context(
    state: WorldState,
    actor_id: UUID,
    *,
    visible_organization_ids: set[UUID],
) -> dict[str, Any]:
    """Return small, current credit facts for an authorized actor.

    ``visible_organization_ids`` comes from the caller's ordinary visibility
    projection. The actor's own signing authority is derived from the current
    governance records directly, so missing self-membership metadata in that
    projection does not hide a role the domain recognizes.
    """

    empty: dict[str, Any] = {
        "snapshot_tick": state.current_tick,
        "represented_institutions": [],
        "represented_institutions_truncated": False,
        "visible_credit_counterparties": [],
        "visible_credit_counterparties_truncated": False,
        "institutional_credit_contracts": [],
        "institutional_credit_contracts_truncated": False,
    }
    if not isinstance(actor_id, UUID):
        return empty
    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in _PERSON_KINDS or actor.status != "active":
        return empty

    # This is a single, shallow candidate pass. Full governance validation is
    # only done for organizations whose serialized membership or office data
    # mentions this actor; if no candidate is authorized, skip all later scans.
    signer_organizations: list[WorldEntity] = []
    for organization in state.entities.values():
        if (
            organization.kind not in _ORGANIZATION_KINDS
            or organization.status != "active"
            or not _raw_governance_mentions_actor(organization, actor_id)
        ):
            continue
        try:
            require_institutional_credit_signer(state, organization.id, actor_id)
        except (TypeError, ValueError):
            continue
        signer_organizations.append(organization)
    signer_organizations.sort(key=lambda entity: str(entity.id))
    if not signer_organizations:
        return empty

    signer_ids = {entity.id for entity in signer_organizations}
    currency_cache: dict[UUID, tuple[list[str], bool]] = {}

    def currencies_for(entity: WorldEntity) -> tuple[list[str], bool]:
        cached = currency_cache.get(entity.id)
        if cached is not None:
            return cached
        values: list[str] = []
        try:
            treasury = TreasuryComponent.model_validate(entity.components.get("treasury"))
        except (TypeError, ValueError, ValidationError):
            result = (values, False)
            currency_cache[entity.id] = result
            return result
        for resource_id, account_id in sorted(treasury.accounts.items()):
            account = state.resources.get(account_id)
            # Check only account registration and ownership. Balance values are
            # intentionally never inspected or projected here.
            try:
                valid_account = isinstance(account, ResourceAccount) and (
                    account.account_type == "holder"
                    and account.owner_id == entity.id
                    and account.resource_id == resource_id
                    and account.credit_limit is None
                )
            except (AttributeError, TypeError, ValueError):
                valid_account = False
            if valid_account:
                values.append(resource_id)
        result = (values, len(values) > MAX_CURRENCIES_PER_ROW)
        currency_cache[entity.id] = result
        return result

    represented_rows: list[dict[str, Any]] = []
    for organization in signer_organizations[: MAX_CONTEXT_ROWS + 1]:
        currencies, currencies_truncated = currencies_for(organization)
        row: dict[str, Any] = {
            "organization_id": str(organization.id),
            "name": _safe_display_name(organization),
            "currency_resource_ids": currencies[:MAX_CURRENCIES_PER_ROW],
            "currency_resource_ids_truncated": currencies_truncated,
        }
        represented_rows.append(row)
    represented_truncated = len(signer_organizations) > MAX_CONTEXT_ROWS
    represented_rows = represented_rows[:MAX_CONTEXT_ROWS]
    represented_for_options = signer_organizations[:MAX_CONTEXT_ROWS]

    visible_ids = {
        organization_id
        for organization_id in visible_organization_ids
        if isinstance(organization_id, UUID)
    } if isinstance(visible_organization_ids, set) else set()
    counterparty_rows = _counterparty_rows(
        state,
        represented_for_options,
        visible_ids,
        currencies_for,
    )

    contracts = _contract_rows(state, actor_id, signer_ids, signer_organizations)

    return {
        "snapshot_tick": state.current_tick,
        "represented_institutions": represented_rows,
        "represented_institutions_truncated": represented_truncated,
        "visible_credit_counterparties": counterparty_rows[:MAX_CONTEXT_ROWS],
        "visible_credit_counterparties_truncated": len(counterparty_rows) > MAX_CONTEXT_ROWS,
        "institutional_credit_contracts": contracts[:MAX_CONTEXT_ROWS],
        "institutional_credit_contracts_truncated": len(contracts) > MAX_CONTEXT_ROWS,
    }


def _raw_governance_mentions_actor(organization: WorldEntity, actor_id: UUID) -> bool:
    raw = organization.components.get("governance")
    if not isinstance(raw, dict):
        return False
    actor_text = str(actor_id)
    members = raw.get("members")
    if isinstance(members, list) and actor_text in members:
        return True
    if raw.get("leader_id") == actor_text:
        return True
    offices = raw.get("offices")
    return isinstance(offices, list) and any(
        isinstance(office, dict) and office.get("holder_id") == actor_text
        for office in offices
    )


def _counterparty_rows(
    state: WorldState,
    represented: list[WorldEntity],
    visible_ids: set[UUID],
    currencies_for: Callable[[WorldEntity], tuple[list[str], bool]],
) -> list[dict[str, Any]]:
    counterparties: list[WorldEntity] = []
    represented_ids = {entity.id for entity in represented}
    for organization_id in visible_ids:
        if organization_id in represented_ids:
            continue
        entity = state.entities.get(organization_id)
        if (
            entity is None
            or entity.kind not in _ORGANIZATION_KINDS
            or entity.status != "active"
            or entity.components.get("hidden") is True
        ):
            continue
        counterparties.append(entity)
    counterparties.sort(key=lambda entity: str(entity.id))

    def rows():
        for own in represented:
            own_currencies, _ = currencies_for(own)
            if not own_currencies:
                continue
            own_currency_set = set(own_currencies)
            for other in counterparties:
                other_currencies, _ = currencies_for(other)
                shared = sorted(own_currency_set.intersection(other_currencies))
                if not shared:
                    continue
                yield {
                    "represented_organization_id": str(own.id),
                    "counterparty_organization_id": str(other.id),
                    "counterparty_name": _safe_display_name(other),
                    "shared_currency_resource_ids": shared[:MAX_CURRENCIES_PER_ROW],
                    "shared_currency_resource_ids_truncated": len(shared) > MAX_CURRENCIES_PER_ROW,
                }
    return nsmallest(
        MAX_CONTEXT_ROWS + 1,
        rows(),
        key=lambda row: (
            row["represented_organization_id"],
            row["counterparty_organization_id"],
        ),
    )


def _contract_rows(
    state: WorldState,
    actor_id: UUID,
    signer_ids: set[UUID],
    signer_organizations: list[WorldEntity],
) -> list[dict[str, Any]]:
    organization_by_id = {entity.id: entity for entity in signer_organizations}

    def row_for(contract: InstitutionalCreditContract) -> dict[str, Any] | None:
        if (
            contract.lender_organization_id not in signer_ids
            and contract.borrower_organization_id not in signer_ids
        ):
            return None
        lender_can_sign = contract.lender_organization_id in signer_ids
        borrower_can_sign = contract.borrower_organization_id in signer_ids
        tick = state.current_tick
        offered = contract.status == "offered"
        can_accept = (
            offered
            and borrower_can_sign
            and tick <= contract.accepted_through_tick
            and tick < contract.maturity_tick
        )
        can_pay = (
            borrower_can_sign
            and contract.remaining_total_minor > 0
            and (
                (contract.status == "active" and tick <= contract.maturity_tick)
                or (
                    contract.status == "overdue"
                    and contract.overdue_at_tick is not None
                    and tick >= contract.overdue_at_tick
                )
            )
        )
        lender = organization_by_id.get(contract.lender_organization_id) or state.entities.get(
            contract.lender_organization_id
        )
        borrower = organization_by_id.get(contract.borrower_organization_id) or state.entities.get(
            contract.borrower_organization_id
        )
        result: dict[str, Any] = {
            "credit_id": str(contract.credit_id),
            "status": contract.status,
            "phase": "offer_expired" if offered and tick > contract.accepted_through_tick else _PHASES[contract.status],
            "revision": contract.revision,
            "lender_organization_id": str(contract.lender_organization_id),
            "borrower_organization_id": str(contract.borrower_organization_id),
            "currency_resource_id": contract.currency_resource_id,
            "principal_minor": str(contract.principal_minor),
            "total_due_minor": str(contract.total_due_minor),
            "repaid_minor": str(contract.repaid_minor),
            "remaining_total_minor": str(contract.remaining_total_minor),
            "accepted_through_tick": contract.accepted_through_tick,
            "maturity_tick": contract.maturity_tick,
            "can_accept": can_accept,
            "can_decline": offered and borrower_can_sign,
            "can_withdraw": offered and lender_can_sign,
            "can_pay": can_pay,
        }
        if lender is not None and _safe_display_name(lender) is not None:
            result["lender_name"] = _safe_display_name(lender)
        if borrower is not None and _safe_display_name(borrower) is not None:
            result["borrower_name"] = _safe_display_name(borrower)
        if contract.accepted_tick is not None:
            result["accepted_tick"] = contract.accepted_tick
        if contract.overdue_at_tick is not None:
            result["overdue_at_tick"] = contract.overdue_at_tick
        result.update(_credit_extension_rows(contract, tick, actor_id, signer_ids, (lender, borrower)))
        return result

    def projected_rows():
        for raw in state.institutional_credit_contracts:
            try:
                contract = raw if isinstance(raw, InstitutionalCreditContract) else InstitutionalCreditContract.model_validate(raw)
                projected_row = row_for(contract)
            except (TypeError, ValueError, KeyError):
                continue
            if projected_row is not None:
                yield projected_row

    def attention_order(row: dict[str, Any]) -> tuple[int, int, str]:
        phase = row["phase"]
        priority = _CONTRACT_ATTENTION_ORDER.get(phase, 3)
        deadline = row["accepted_through_tick"] if phase == "offer_pending" else row["maturity_tick"]
        return priority, deadline if phase in _CONTRACT_ATTENTION_ORDER else -deadline, row["credit_id"]

    return nsmallest(MAX_CONTEXT_ROWS + 1, projected_rows(), key=attention_order)


def _credit_extension_rows(contract, tick, actor_id, signer_ids, parties) -> dict[str, Any]:
    funded = contract.status in {"active", "overdue"}
    latest = contract.extensions[-1] if contract.extensions else None
    live = funded and latest is not None and latest.status == "proposed" and tick <= latest.accept_through_tick
    rows = []
    for proposal in reversed(contract.extensions[-MAX_EXTENSIONS_PER_CONTRACT:]):
        other_id = (contract.borrower_organization_id if proposal.proposer_organization_id == contract.lender_organization_id
                    else contract.lender_organization_id)
        current = live and proposal == latest
        can_respond = current and other_id in signer_ids and actor_id != proposal.proposer_actor_id
        row = {
            "extension_id": str(proposal.extension_id), "proposer_organization_id": str(proposal.proposer_organization_id),
            "previous_maturity_tick": proposal.previous_maturity_tick, "new_maturity_tick": proposal.new_maturity_tick,
            "accept_through_tick": proposal.accept_through_tick, "proposed_tick": proposal.proposed_tick,
            "status": proposal.status, "expired": proposal.status == "proposed" and not current,
            "can_accept": can_respond, "can_decline": can_respond,
            "can_withdraw": current and proposal.proposer_organization_id in signer_ids,
        }
        if proposal.resolved_tick is not None:
            row["resolved_tick"] = proposal.resolved_tick
        if proposal.previous_overdue_tick is not None:
            row["previous_overdue_tick"] = proposal.previous_overdue_tick
        rows.append(row)
    options = [
        {"organization_id": str(entity.id), "name": _safe_display_name(entity)}
        for entity in parties if entity is not None and entity.id in signer_ids
    ] if funded and not live else []
    return {"extension_options": options, "extensions": rows,
            "extensions_truncated": len(contract.extensions) > MAX_EXTENSIONS_PER_CONTRACT}


def _safe_display_name(entity: WorldEntity) -> str | None:
    if entity.components.get("hidden") is True:
        return None
    name = entity.name
    if not isinstance(name, str) or not name.strip():
        return None
    return name
