"""Authoritative treasury settlement and maturity projection for term credit."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from .components import ScheduledEventRecord
from .errors import InvalidEvent
from .institutional_credit import (
    InstitutionalCreditContract,
    accept_institutional_credit,
    decline_institutional_credit,
    mark_institutional_credit_overdue,
    pay_institutional_credit,
    propose_institutional_credit,
    withdraw_institutional_credit,
)
from .institutional_credit_events import (
    InstitutionalCreditChangedEvent, InstitutionalCreditChangedOperation, InstitutionalCreditTransfer,
    institutional_credit_event_id,
)

if TYPE_CHECKING:
    from .models import WorldState


CREDIT_MATURITY_HANDLER = "economy.credit_maturity"
CREDIT_MATURITY_HANDLER_VERSION = "1.0.0"
CREDIT_OFFER_COMMAND = "action.offer_institutional_credit"
CREDIT_RESOLUTION_COMMAND = "action.resolve_institutional_credit"
_PARTY_KINDS = {"organization", "polity", "faction", "company", "government"}
_RESOLVERS = {
    "accept": accept_institutional_credit,
    "decline": decline_institutional_credit,
    "withdraw": withdraw_institutional_credit,
}


def credit_maturity_schedule_id(credit_id: UUID, accepted_extension_id: UUID | None = None) -> UUID:
    handler_key = CREDIT_MATURITY_HANDLER
    if accepted_extension_id is not None:
        handler_key = f"{handler_key}:{accepted_extension_id}"
    return uuid5(credit_id, handler_key)


def credit_maturity_schedule(contract: InstitutionalCreditContract) -> ScheduledEventRecord:
    latest_extension = next((row.extension_id for row in reversed(contract.extensions) if row.status == "accepted"), None)
    return ScheduledEventRecord(
        id=credit_maturity_schedule_id(contract.credit_id, latest_extension), due_tick=contract.maturity_tick + 1,
        phase="maintenance", handler_id=CREDIT_MATURITY_HANDLER,
        handler_version=CREDIT_MATURITY_HANDLER_VERSION, payload={"credit_id": str(contract.credit_id)},
    )


def resolve_institutional_credit_transition(
    contract: InstitutionalCreditContract, operation: InstitutionalCreditChangedOperation, *,
    actor_organization_id: UUID | None, current_tick: int, expected_revision: int,
    amount_minor: int | None = None,
) -> InstitutionalCreditContract:
    if operation == "overdue":
        return mark_institutional_credit_overdue(
            contract, actor_organization_id=contract.lender_organization_id,
            current_tick=current_tick, expected_revision=expected_revision,
        )
    if actor_organization_id is None:
        raise InvalidEvent("Institutional credit decisions must identify the signing institution.")
    if operation == "pay":
        if amount_minor is None:
            raise InvalidEvent("Institutional credit payments require an amount.")
        return pay_institutional_credit(
            contract, actor_organization_id=actor_organization_id, amount_minor=amount_minor,
            current_tick=current_tick, expected_revision=expected_revision,
        )
    return _RESOLVERS[operation](
        contract, actor_organization_id=actor_organization_id, current_tick=current_tick,
        expected_revision=expected_revision,
    )


def credit_transfer_for_operation(
    state: WorldState, contract: InstitutionalCreditContract, operation: InstitutionalCreditChangedOperation,
    amount_minor: int | None,
) -> InstitutionalCreditTransfer | None:
    from .institutional_credit_treasury import plan_institutional_credit_transfer

    lender, borrower = contract.lender_organization_id, contract.borrower_organization_id
    if operation == "accept":
        debit, credit, amount = lender, borrower, contract.principal_minor
    elif operation == "pay" and amount_minor is not None:
        debit, credit, amount = borrower, lender, amount_minor
    else:
        return None
    return plan_institutional_credit_transfer(
        state, debit_organization_id=debit, credit_organization_id=credit,
        currency_resource_id=contract.currency_resource_id, amount_minor=amount,
    )


def institutional_credit_command_payload(event: InstitutionalCreditChangedEvent) -> dict[str, object]:
    contract = event.resulting_contract
    if event.operation == "offer":
        terms = contract.model_dump(mode="json", include={
            "lender_organization_id", "borrower_organization_id", "currency_resource_id", "principal_minor",
            "total_repayable_minor", "accepted_through_tick", "maturity_tick",
        })
        return {"kind": "offer_institutional_credit", **terms}
    if event.operation == "overdue":
        raise InvalidEvent("Institutional credit maturity uses the simulation job payload.")
    payload = {
        "kind": "resolve_institutional_credit", "credit_id": str(contract.credit_id),
        "operation": event.operation, "expected_revision": event.expected_revision,
    }
    if event.transfer is not None and event.operation == "pay":
        payload["amount_minor"] = event.transfer.amount_minor
    return payload


def validate_institutional_credit_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str, command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    event = next((item for item in events if isinstance(item, InstitutionalCreditChangedEvent)), None)
    if event is None:
        if command_type in {CREDIT_OFFER_COMMAND, CREDIT_RESOLUTION_COMMAND}:
            raise InvalidEvent("Institutional credit commands require their typed event.")
        return
    if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("Institutional credit must match a single scoped command.")
    if command_type is None:
        return
    if event.operation == "overdue":
        if command_type != "simulation.window" or (command_payload or {}).get("next_tick") != str(event.resulting_contract.updated_tick):
            raise InvalidEvent("Institutional arrears require a simulation command at the maturity tick.")
        return
    expected_type = CREDIT_OFFER_COMMAND if event.operation == "offer" else CREDIT_RESOLUTION_COMMAND
    if command_type != expected_type or command_payload != institutional_credit_command_payload(event):
        raise InvalidEvent("The institutional credit event does not match its command proposal.")


def plan_institutional_credit_maturity_event(
    state: WorldState, schedule: ScheduledEventRecord, *, world_id: UUID, branch_id: UUID,
    command_id: UUID, actor_id: UUID, ruleset_version: str,
) -> InstitutionalCreditChangedEvent:
    contract = next((row for row in state.institutional_credit_contracts
                     if str(row.credit_id) == schedule.payload.get("credit_id")), None)
    if contract is None or schedule != credit_maturity_schedule(contract) or schedule.due_tick != state.current_tick:
        raise InvalidEvent("Institutional credit maturity does not match a current contract boundary.")
    result = mark_institutional_credit_overdue(
        contract, actor_organization_id=contract.lender_organization_id,
        current_tick=state.current_tick, expected_revision=contract.revision,
    )
    return InstitutionalCreditChangedEvent(
        event_id=institutional_credit_event_id(command_id, ruleset_version, "overdue", contract.credit_id),
        world_id=world_id, branch_id=branch_id, command_id=command_id, actor_id=actor_id,
        ruleset_version=ruleset_version, operation="overdue", expected_revision=contract.revision,
        resulting_contract=result, visibility={"policy": "author_only"},
    )


def validate_institutional_credit_state(state: WorldState) -> None:
    """Tie saved debts and their maturity handlers to existing institutions."""
    schedules = {row.id: row for row in state.scheduled_events if row.handler_id == CREDIT_MATURITY_HANDLER}
    seen: set[UUID] = set()
    for contract in state.institutional_credit_contracts:
        if contract.credit_id in seen or contract.updated_tick > state.current_tick:
            raise ValueError("Institutional credit IDs must be unique and committed by the current tick.")
        seen.add(contract.credit_id)
        for party_id in (contract.lender_organization_id, contract.borrower_organization_id):
            party = state.entities.get(party_id)
            if party is None or party.kind not in _PARTY_KINDS:
                raise ValueError("Institutional credit requires two registered institution parties.")
        if contract.accepted_tick is None:
            continue
        expected = current_credit_maturity_record(contract)
        if schedules.pop(expected.id, None) != expected:
            raise ValueError("Institutional credit maturity must match its canonical schedule.")
    if schedules:
        raise ValueError("Institutional credit maturity schedules require a registered funded contract.")


def current_credit_maturity_record(contract: InstitutionalCreditContract) -> ScheduledEventRecord:
    expected = credit_maturity_schedule(contract)
    if contract.overdue_at_tick is not None:
        if contract.overdue_at_tick != expected.due_tick:
            raise ValueError("Institutional credit must record arrears at the exact maturity boundary.")
        return expected.model_copy(update={"status": "completed", "completed_runs": 1, "revision": 2})
    if contract.status == "settled":
        return expected.model_copy(update={"status": "cancelled", "revision": 2})
    return expected


def apply_institutional_credit_event(state: WorldState, event: InstitutionalCreditChangedEvent) -> dict[str, object]:
    """Recompute an event's change and return the patches for one atomic projection."""
    from .institutional_credit_treasury import (
        apply_institutional_credit_transfer,
        require_institutional_credit_signer,
    )

    result = event.resulting_contract
    contracts = state.institutional_credit_contracts
    previous = next((row for row in contracts if row.credit_id == result.credit_id), None)
    operation = event.operation
    if operation != "overdue":
        require_institutional_credit_signer(state, event.actor_organization_id, event.actor_id)
    if operation == "offer":
        if previous is not None:
            raise InvalidEvent("Institutional credit offer ID already exists.")
        borrower = state.entities.get(result.borrower_organization_id)
        if borrower is None or borrower.kind not in _PARTY_KINDS or borrower.status != "active":
            raise InvalidEvent("Institutional credit offers require an active borrower institution.")
        expected = propose_institutional_credit(
            credit_id=result.credit_id, lender_organization_id=result.lender_organization_id,
            borrower_organization_id=result.borrower_organization_id, currency_resource_id=result.currency_resource_id,
            principal_minor=result.principal_minor, total_repayable_minor=result.total_repayable_minor,
            accepted_through_tick=result.accepted_through_tick, maturity_tick=result.maturity_tick,
            current_tick=state.current_tick,
        )
        if result.maturity_tick == 2**63 - 1:
            raise InvalidEvent("Institutional credit must leave a representable tick for its maturity check.")
    elif previous is None:
        raise InvalidEvent("The institutional credit contract does not exist.")
    else:
        expected = resolve_institutional_credit_transition(
            previous, operation, actor_organization_id=event.actor_organization_id,
            current_tick=state.current_tick, expected_revision=event.expected_revision,
            amount_minor=event.transfer.amount_minor if event.transfer is not None else None,
        )
    if result != expected:
        raise InvalidEvent("Institutional credit event does not match its authoritative transition.")

    patches: dict[str, object] = {"institutional_credit_contracts": (
        [*contracts, result] if previous is None else [result if row.credit_id == result.credit_id else row for row in contracts]
    )}
    if event.transfer is not None:
        transfer = credit_transfer_for_operation(state, result, operation, event.transfer.amount_minor)
        if event.transfer != transfer:
            raise InvalidEvent("The credit transfer does not use the institutions' current treasury accounts.")
        patches["resources"] = state.resources | apply_institutional_credit_transfer(state, transfer)

    if operation == "accept":
        schedule = credit_maturity_schedule(result)
        if any(row.id == schedule.id for row in state.scheduled_events):
            raise InvalidEvent("Institutional credit maturity schedule ID is already in use.")
        patches["scheduled_events"] = [*state.scheduled_events, schedule]
    if operation == "overdue" or (operation == "pay" and previous.status == "active" and result.status == "settled"):
        schedule = credit_maturity_schedule(previous)
        if schedule not in state.scheduled_events:
            raise InvalidEvent("Institutional credit maturity schedule is no longer queued as expected.")
        if operation == "overdue" and schedule.due_tick != state.current_tick:
            raise InvalidEvent("Institutional credit arrears require the exact scheduled tick.")
        closed = schedule.model_copy(update={
            "status": "completed" if operation == "overdue" else "cancelled",
            "completed_runs": int(operation == "overdue"), "revision": schedule.revision + 1,
        })
        patches["scheduled_events"] = [closed if row.id == schedule.id else row for row in state.scheduled_events]
    return patches
