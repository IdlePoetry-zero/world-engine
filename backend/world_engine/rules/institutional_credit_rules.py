"""Translate authorized financial choices to the institutional credit event."""

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.institutional_credit import propose_institutional_credit
from world_engine.domain.institutional_credit_extension import propose_credit_extension, resolve_credit_extension
from world_engine.domain.institutional_credit_extension_events import (
    InstitutionalCreditExtensionChangedEvent, credit_extension_event_id, credit_extension_id,
)
from world_engine.domain.institutional_credit_extension_state import apply_credit_extension_event
from world_engine.domain.institutional_credit_events import (
    InstitutionalCreditChangedEvent, institutional_credit_event_id, institutional_credit_offer_id,
)
from world_engine.domain.institutional_credit_state import (
    apply_institutional_credit_event, credit_transfer_for_operation, resolve_institutional_credit_transition,
)
from world_engine.domain.institutional_credit_treasury import require_institutional_credit_signer
from world_engine.domain.institutional_treasury_reserve import plan_reserve_mobilization
from world_engine.domain.institutional_treasury_reserve_events import (
    InstitutionalTreasuryMobilizedEvent, institutional_treasury_mobilized_event_id,
)
from world_engine.domain.models import WorldState

from .contracts import (
    MobilizeTreasuryReserveIntent, OfferInstitutionalCreditIntent, ProposeCreditExtensionIntent,
    ResolveCreditExtensionIntent, ResolveInstitutionalCreditIntent, RuleContext,
)


def plan_institutional_credit_action(
    state: WorldState, intent: OfferInstitutionalCreditIntent | ResolveInstitutionalCreditIntent,
    context: RuleContext,
) -> InstitutionalCreditChangedEvent:
    amount_minor = None
    if isinstance(intent, OfferInstitutionalCreditIntent):
        actor_organization_id = intent.lender_organization_id
        require_institutional_credit_signer(state, actor_organization_id, context.actor_id)
        operation, expected_revision = "offer", 0
        result = propose_institutional_credit(
            credit_id=institutional_credit_offer_id(context.command_id), current_tick=state.current_tick,
            **intent.model_dump(mode="python", exclude={"kind"}),
        )
    else:
        contract = next((row for row in state.institutional_credit_contracts if row.credit_id == intent.credit_id), None)
        if contract is None:
            raise InvalidEvent("Institutional credit is unavailable for this actor.")
        actor_organization_id = (
            contract.lender_organization_id if intent.operation == "withdraw" else contract.borrower_organization_id
        )
        require_institutional_credit_signer(state, actor_organization_id, context.actor_id)
        operation, expected_revision, amount_minor = intent.operation, intent.expected_revision, intent.amount_minor
        result = resolve_institutional_credit_transition(
            contract, operation, actor_organization_id=actor_organization_id, current_tick=state.current_tick,
            expected_revision=expected_revision, amount_minor=amount_minor,
        )
    event = InstitutionalCreditChangedEvent(
        event_id=institutional_credit_event_id(context.command_id, context.ruleset_version, operation, result.credit_id),
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
        actor_id=context.actor_id, actor_organization_id=actor_organization_id, ruleset_version=context.ruleset_version,
        operation=operation, expected_revision=expected_revision, resulting_contract=result,
        transfer=credit_transfer_for_operation(state, result, operation, amount_minor),
        visibility={"policy": "author_only"},
    )
    apply_institutional_credit_event(state, event)
    return event


def plan_institutional_reserve_action(
    state: WorldState, intent: MobilizeTreasuryReserveIntent, context: RuleContext,
) -> InstitutionalTreasuryMobilizedEvent:
    destination, allocations = plan_reserve_mobilization(
        state, organization_id=intent.organization_id, actor_id=context.actor_id,
        currency_resource_id=intent.currency_resource_id, amount_minor=intent.amount_minor,
    )
    return InstitutionalTreasuryMobilizedEvent(
        event_id=institutional_treasury_mobilized_event_id(
            context.command_id, context.ruleset_version, intent.organization_id,
        ),
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
        actor_id=context.actor_id, organization_id=intent.organization_id,
        ruleset_version=context.ruleset_version, currency_resource_id=intent.currency_resource_id,
        amount_minor=intent.amount_minor, destination_account_id=destination, allocations=allocations,
        visibility={"policy": "author_only"},
    )


def plan_institutional_credit_extension(
    state: WorldState, intent: ProposeCreditExtensionIntent | ResolveCreditExtensionIntent, context: RuleContext,
) -> InstitutionalCreditExtensionChangedEvent:
    contract = next((row for row in state.institutional_credit_contracts if row.credit_id == intent.credit_id), None)
    if contract is None:
        raise InvalidEvent("Credit extension requires a registered loan.")
    parameters = {"actor_id": context.actor_id, "expected_revision": intent.expected_revision, "current_tick": state.current_tick}
    if isinstance(intent, ProposeCreditExtensionIntent):
        organization_id = intent.organization_id
        extension_id, operation = credit_extension_id(context.command_id), "propose"
        require_institutional_credit_signer(state, organization_id, context.actor_id)
        result = propose_credit_extension(
            contract, **parameters, extension_id=extension_id, actor_organization_id=organization_id,
            new_maturity_tick=intent.new_maturity_tick, accept_through_tick=intent.accept_through_tick,
        )
    else:
        proposal = contract.extensions[-1] if contract.extensions else None
        if proposal is None or proposal.extension_id != intent.extension_id:
            raise InvalidEvent("This credit extension proposal is unavailable.")
        proposer = proposal.proposer_organization_id
        counterpart = (contract.borrower_organization_id if proposer == contract.lender_organization_id
                       else contract.lender_organization_id)
        extension_id, operation = intent.extension_id, intent.operation
        organization_id = proposer if operation == "withdraw" else counterpart
        require_institutional_credit_signer(state, organization_id, context.actor_id)
        result = resolve_credit_extension(
            contract, **parameters, extension_id=extension_id, actor_organization_id=organization_id, operation=operation,
        )
    event = InstitutionalCreditExtensionChangedEvent(
        event_id=credit_extension_event_id(context.command_id, context.ruleset_version, extension_id),
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
        actor_id=context.actor_id, organization_id=organization_id, ruleset_version=context.ruleset_version,
        operation=operation, extension_id=extension_id, expected_revision=intent.expected_revision,
        resulting_contract=result,
    )
    apply_credit_extension_event(state, event)
    return event
