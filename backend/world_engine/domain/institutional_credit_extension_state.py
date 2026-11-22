"""Recompute approved credit extensions and their maturity boundary on replay."""

from .errors import InvalidEvent
from .institutional_credit_extension import propose_credit_extension, resolve_credit_extension
from .institutional_credit_extension_events import InstitutionalCreditExtensionChangedEvent
from .institutional_credit_state import credit_maturity_schedule, current_credit_maturity_record
from .institutional_credit_treasury import require_institutional_credit_signer
from .models import WorldState


def apply_credit_extension_event(state: WorldState, event: InstitutionalCreditExtensionChangedEvent) -> dict[str, object]:
    credit_id = event.resulting_contract.credit_id
    contract = next((row for row in state.institutional_credit_contracts if row.credit_id == credit_id), None)
    if contract is None:
        raise InvalidEvent("Credit extension must refer to a registered loan.")
    require_institutional_credit_signer(state, event.organization_id, event.actor_id)
    context = {
        "extension_id": event.extension_id, "actor_id": event.actor_id, "actor_organization_id": event.organization_id,
        "current_tick": state.current_tick, "expected_revision": event.expected_revision,
    }
    if event.operation == "propose":
        proposal = event.resulting_contract.extensions[-1]
        expected = propose_credit_extension(
            contract, **context, new_maturity_tick=proposal.new_maturity_tick, accept_through_tick=proposal.accept_through_tick,
        )
    else:
        expected = resolve_credit_extension(contract, **context, operation=event.operation)
    if expected != event.resulting_contract:
        raise InvalidEvent("Credit extension receipt must match its authoritative transition.")
    changes: dict[str, object] = {
        "institutional_credit_contracts": [expected if row.credit_id == credit_id else row for row in state.institutional_credit_contracts],
    }
    if event.operation == "accept":
        previous_schedule = current_credit_maturity_record(contract)
        next_schedule = credit_maturity_schedule(expected)
        if previous_schedule not in state.scheduled_events or any(
            scheduled.id == next_schedule.id for scheduled in state.scheduled_events
        ):
            raise InvalidEvent("Credit extension requires the previous boundary and a distinct next boundary.")
        changes["scheduled_events"] = [
            next_schedule if scheduled.id == previous_schedule.id else scheduled for scheduled in state.scheduled_events
        ]
    return changes
