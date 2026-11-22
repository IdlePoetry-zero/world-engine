"""Pure, immutable lifecycle for proposed institutional credit extensions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1

if TYPE_CHECKING:
    from .institutional_credit import InstitutionalCreditContract

CreditExtensionStatus = Literal["proposed", "accepted", "declined", "withdrawn"]
CreditExtensionOperation = Literal["accept", "decline", "withdraw"]


class InstitutionalCreditExtension(BaseModel):
    """One bilateral proposal record retained in contract history."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    extension_id: UUID
    proposer_organization_id: UUID
    proposer_actor_id: UUID
    previous_maturity_tick: StrictInt = Field(ge=_MIN_TICK, lt=_MAX_TICK)
    new_maturity_tick: StrictInt = Field(ge=_MIN_TICK, lt=_MAX_TICK)
    accept_through_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    proposed_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    status: CreditExtensionStatus = "proposed"
    resolved_tick: StrictInt | None = Field(default=None, ge=_MIN_TICK, le=_MAX_TICK)
    resolved_by_actor_id: UUID | None = None
    previous_overdue_tick: StrictInt | None = Field(default=None, ge=_MIN_TICK, le=_MAX_TICK)

    @model_validator(mode="after")
    def valid_extension_history_row(self) -> InstitutionalCreditExtension:
        if self.new_maturity_tick <= max(self.previous_maturity_tick, self.proposed_tick):
            raise ValueError("an extension maturity must follow both its prior maturity and proposal tick")
        if not self.proposed_tick <= self.accept_through_tick < self.new_maturity_tick:
            raise ValueError("an extension acceptance deadline must include proposal time and precede new maturity")
        resolved = self.resolved_tick is not None
        if resolved != (self.resolved_by_actor_id is not None):
            raise ValueError("resolved extensions require both a resolution tick and resolving actor")
        if self.status == "proposed":
            if resolved or self.previous_overdue_tick is not None:
                raise ValueError("a pending extension cannot include resolution or a carried overdue fact")
        else:
            if not resolved or not self.proposed_tick <= self.resolved_tick <= self.accept_through_tick:
                raise ValueError("a resolved extension must resolve within its inclusive acceptance window")
            if self.status == "accepted":
                expected_overdue_tick = (
                    self.previous_maturity_tick + 1
                    if self.resolved_tick > self.previous_maturity_tick
                    else None
                )
                if self.previous_overdue_tick != expected_overdue_tick:
                    raise ValueError("accepted extension must retain any prior overdue breach exactly")
                if self.resolved_by_actor_id == self.proposer_actor_id:
                    raise ValueError("extension acceptance requires a different character from the proposer")
            elif self.previous_overdue_tick is not None:
                raise ValueError("only accepted extensions may copy a prior overdue breach")
            elif self.resolved_by_actor_id == self.proposer_actor_id and self.status == "declined":
                raise ValueError("extension decline requires a different character from the proposer")
        if self.previous_overdue_tick is not None and (
            self.previous_overdue_tick != self.previous_maturity_tick + 1
            or self.previous_overdue_tick > self.resolved_tick
        ):
            raise ValueError("an extension must retain the exact prior overdue boundary")
        return self


def validate_credit_extension_history(
    extensions: tuple[InstitutionalCreditExtension, ...],
    *,
    maturity_tick: int,
    status: str,
    accepted_tick: int | None,
    accepted_through_tick: int,
    updated_tick: int,
    lender_organization_id: UUID,
    borrower_organization_id: UUID,
) -> None:
    """Check ordered proposal history and that accepted maturities form the current chain."""
    if not extensions:
        return
    if accepted_tick is None or status not in {"active", "overdue", "settled"}:
        raise ValueError("credit extensions require an accepted institutional credit")
    if extensions[0].previous_maturity_tick <= accepted_through_tick:
        raise ValueError("initial extension history must follow the original offer cutoff")

    extension_ids = [row.extension_id for row in extensions]
    if len(extension_ids) != len(set(extension_ids)):
        raise ValueError("institutional credit extension IDs must be unique")

    expected_maturity = extensions[0].previous_maturity_tick
    previous: InstitutionalCreditExtension | None = None
    for row in extensions:
        if row.proposer_organization_id not in {lender_organization_id, borrower_organization_id}:
            raise ValueError("credit extension proposer must be a contract party")
        if row.proposed_tick < accepted_tick:
            raise ValueError("credit extension proposal cannot precede the original acceptance")
        if row.proposed_tick > updated_tick or (
            row.resolved_tick is not None and row.resolved_tick > updated_tick
        ):
            raise ValueError("credit extension history cannot be later than the contract update")
        if row.previous_maturity_tick != expected_maturity:
            raise ValueError("institutional credit extension maturities must form an ordered chain")
        if previous is not None:
            if row.proposed_tick < previous.proposed_tick:
                raise ValueError("institutional credit extensions must be stored in proposal-time order")
            if previous.status == "proposed":
                if row.proposed_tick <= previous.accept_through_tick:
                    raise ValueError("a new extension cannot overlap a still-live proposal")
            elif row.proposed_tick < previous.resolved_tick:
                raise ValueError("a new extension cannot precede the preceding resolution")
        if row.status == "accepted":
            expected_maturity = row.new_maturity_tick
        previous = row

    if maturity_tick != expected_maturity:
        raise ValueError("current credit maturity must match the accepted extension history")


def propose_credit_extension(
    contract: InstitutionalCreditContract,
    *,
    extension_id: UUID,
    actor_organization_id: UUID,
    actor_id: UUID,
    current_tick: int,
    expected_revision: int,
    new_maturity_tick: int,
    accept_through_tick: int,
) -> InstitutionalCreditContract:
    """Append a lender- or borrower-proposed maturity extension."""
    from .institutional_credit import (
        InstitutionalCreditContract,
        InstitutionalCreditError,
        _advance,
        _require_tick,
        _require_transition,
    )

    if not isinstance(contract, InstitutionalCreditContract):
        raise InstitutionalCreditError("invalid_contract", "A typed institutional credit contract is required.")
    _require_transition(contract, actor_organization_id, current_tick, expected_revision)
    if not isinstance(actor_id, UUID) or not isinstance(extension_id, UUID):
        raise InstitutionalCreditError("invalid_extension_actor", "Credit extension proposal requires known UUID identities.")
    if actor_organization_id not in {contract.lender_organization_id, contract.borrower_organization_id}:
        raise InstitutionalCreditError("wrong_organization", "Only a party to the credit may propose an extension.")
    if contract.status not in {"active", "overdue"}:
        raise InstitutionalCreditError("invalid_status", "Only active or overdue credit can be extended.")
    if contract.status == "active" and current_tick > contract.maturity_tick:
        raise InstitutionalCreditError("overdue_not_marked", "Past-maturity credit must record arrears before extension.")
    if contract.status == "overdue" and (
        contract.overdue_at_tick != contract.maturity_tick + 1
        or current_tick < contract.overdue_at_tick
    ):
        raise InstitutionalCreditError("invalid_overdue_tick", "Overdue credit must retain its canonical maturity boundary.")
    _require_tick(new_maturity_tick, "new maturity tick")
    _require_tick(accept_through_tick, "extension acceptance deadline")
    if new_maturity_tick >= 2**63 - 1 or new_maturity_tick <= max(current_tick, contract.maturity_tick):
        raise InstitutionalCreditError(
            "invalid_extension_maturity", "New maturity must be later than both current time and the existing maturity.",
        )
    if not current_tick <= accept_through_tick < new_maturity_tick:
        raise InstitutionalCreditError(
            "invalid_extension_deadline", "The inclusive acceptance deadline must run from now to before new maturity.",
        )
    if any(row.extension_id == extension_id for row in contract.extensions):
        raise InstitutionalCreditError("duplicate_extension", "This extension identity already exists.")
    if any(
        row.status == "proposed" and current_tick <= row.accept_through_tick
        for row in contract.extensions
    ):
        raise InstitutionalCreditError("extension_already_pending", "Another extension proposal is still live.")

    try:
        extension = InstitutionalCreditExtension(
            extension_id=extension_id,
            proposer_organization_id=actor_organization_id,
            proposer_actor_id=actor_id,
            previous_maturity_tick=contract.maturity_tick,
            new_maturity_tick=new_maturity_tick,
            accept_through_tick=accept_through_tick,
            proposed_tick=current_tick,
            status="proposed",
        )
    except (TypeError, ValueError) as error:
        raise InstitutionalCreditError("invalid_extension_terms", "Credit extension proposal terms are invalid.") from error
    return _advance(contract, current_tick, extensions=(*contract.extensions, extension))


def resolve_credit_extension(
    contract: InstitutionalCreditContract,
    *,
    extension_id: UUID,
    actor_organization_id: UUID,
    actor_id: UUID,
    current_tick: int,
    expected_revision: int,
    operation: CreditExtensionOperation,
) -> InstitutionalCreditContract:
    """Accept, decline, or withdraw one live extension proposal."""
    from .institutional_credit import (
        InstitutionalCreditContract,
        InstitutionalCreditError,
        _advance,
        _require_transition,
    )

    if not isinstance(contract, InstitutionalCreditContract):
        raise InstitutionalCreditError("invalid_contract", "A typed institutional credit contract is required.")
    _require_transition(contract, actor_organization_id, current_tick, expected_revision)
    if not isinstance(actor_id, UUID) or not isinstance(extension_id, UUID):
        raise InstitutionalCreditError("invalid_extension_actor", "Credit extension resolution requires known UUID identities.")
    if operation not in {"accept", "decline", "withdraw"}:
        raise InstitutionalCreditError("invalid_extension_operation", "Credit extension operation is unsupported.")
    if contract.status not in {"active", "overdue"}:
        raise InstitutionalCreditError("invalid_status", "Settled or unaccepted credit cannot resolve an extension.")

    index = next((i for i, row in enumerate(contract.extensions) if row.extension_id == extension_id), None)
    if index is None:
        raise InstitutionalCreditError("extension_not_found", "The credit extension proposal was not found.")
    proposal = contract.extensions[index]
    if proposal.status != "proposed":
        raise InstitutionalCreditError("extension_resolved", "The credit extension proposal is already resolved.")
    if current_tick > proposal.accept_through_tick:
        raise InstitutionalCreditError("extension_expired", "The credit extension proposal has expired.")

    if operation == "withdraw":
        if actor_organization_id != proposal.proposer_organization_id:
            raise InstitutionalCreditError("wrong_organization", "Only the proposing organization may withdraw the extension.")
    else:
        if actor_organization_id == proposal.proposer_organization_id:
            raise InstitutionalCreditError("wrong_organization", "Only the counterparty may accept or decline the extension.")
        if actor_id == proposal.proposer_actor_id:
            raise InstitutionalCreditError("same_extension_actor", "A different character must provide counterparty consent.")

    previous_overdue_tick = None
    maturity_tick = contract.maturity_tick
    status = contract.status
    overdue_at_tick = contract.overdue_at_tick
    if operation == "accept":
        if current_tick > contract.maturity_tick:
            if contract.status != "overdue" or contract.overdue_at_tick != contract.maturity_tick + 1:
                raise InstitutionalCreditError(
                    "overdue_not_marked", "A post-maturity extension acceptance must retain the recorded breach boundary.",
                )
            previous_overdue_tick = contract.overdue_at_tick
        elif contract.status == "overdue":
            raise InstitutionalCreditError("invalid_overdue_tick", "Recorded arrears cannot precede maturity.")
        maturity_tick = proposal.new_maturity_tick
        status = "active"
        overdue_at_tick = None

    updated_proposal = InstitutionalCreditExtension.model_validate(
        proposal.model_dump(mode="python") | {
            "status": {
                "accept": "accepted",
                "decline": "declined",
                "withdraw": "withdrawn",
            }[operation],
            "resolved_tick": current_tick,
            "resolved_by_actor_id": actor_id,
            "previous_overdue_tick": previous_overdue_tick,
        }
    )
    history = list(contract.extensions)
    history[index] = updated_proposal
    return _advance(
        contract,
        current_tick,
        maturity_tick=maturity_tick,
        status=status,
        overdue_at_tick=overdue_at_tick,
        extensions=tuple(history),
    )

