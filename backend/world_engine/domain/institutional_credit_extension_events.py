"""Private, command-bound receipts for changing a funded credit deadline."""

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, StrictInt, model_validator

from .errors import InvalidEvent
from .institutional_credit import InstitutionalCreditContract
from .institutional_credit_events import InstitutionalCreditEventModel
from .json_types import JsonObject


CREDIT_EXTENSION_EVENT_TYPE = "core.institutional_credit_extension_changed.v1"
CREDIT_EXTENSION_PROPOSAL_COMMAND = "action.propose_credit_extension"
CREDIT_EXTENSION_RESOLUTION_COMMAND = "action.resolve_credit_extension"


def credit_extension_id(command_id: UUID) -> UUID:
    return uuid5(command_id, "institutional-credit-extension")


def credit_extension_event_id(command_id: UUID, ruleset_version: str, extension_id: UUID) -> UUID:
    return uuid5(command_id, f"{ruleset_version}:institutional_credit.extension:{extension_id}")


class InstitutionalCreditExtensionChangedEvent(InstitutionalCreditEventModel):
    type: Literal["core.institutional_credit_extension_changed.v1"] = CREDIT_EXTENSION_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    organization_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    operation: Literal["propose", "accept", "decline", "withdraw"]
    extension_id: UUID
    expected_revision: StrictInt = Field(ge=1, lt=2**63 - 1)
    resulting_contract: InstitutionalCreditContract
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def validate_receipt(self):
        if self.event_id != credit_extension_event_id(self.command_id, self.ruleset_version, self.extension_id):
            raise ValueError("Credit extension event identity must bind its command and proposal.")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("Credit extension receipts are author_only.")
        if self.resulting_contract.revision != self.expected_revision + 1:
            raise ValueError("Credit extension receipt requires the exact next contract revision.")
        proposals = self.resulting_contract.extensions
        if not proposals or proposals[-1].extension_id != self.extension_id:
            raise ValueError("Credit extension receipt must name the latest proposal.")
        proposal = proposals[-1]
        if self.operation == "propose":
            if (self.extension_id != credit_extension_id(self.command_id) or proposal.status != "proposed"
                or proposal.proposer_organization_id != self.organization_id
                or proposal.proposer_actor_id != self.actor_id):
                raise ValueError("Credit extension proposal does not match its command actor or identity.")
        else:
            expected_status = {"accept": "accepted", "decline": "declined", "withdraw": "withdrawn"}[self.operation]
            if proposal.status != expected_status or proposal.resolved_by_actor_id != self.actor_id:
                raise ValueError("Credit extension resolution does not match its command actor or choice.")
        return self


def credit_extension_command_payload(event: InstitutionalCreditExtensionChangedEvent) -> dict[str, object]:
    contract = event.resulting_contract
    payload: dict[str, object] = {"credit_id": str(contract.credit_id), "expected_revision": event.expected_revision}
    if event.operation == "propose":
        proposal = contract.extensions[-1]
        return payload | {
            "kind": "propose_credit_extension", "organization_id": str(event.organization_id),
            "new_maturity_tick": proposal.new_maturity_tick, "accept_through_tick": proposal.accept_through_tick,
        }
    return payload | {"kind": "resolve_credit_extension", "extension_id": str(event.extension_id), "operation": event.operation}


def validate_credit_extension_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str, command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    event = next((item for item in events if isinstance(item, InstitutionalCreditExtensionChangedEvent)), None)
    if event is None:
        if command_type in {CREDIT_EXTENSION_PROPOSAL_COMMAND, CREDIT_EXTENSION_RESOLUTION_COMMAND}:
            raise InvalidEvent("Credit extension commands require their typed receipt.")
        return
    if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("Credit extension requires a single matching scoped command.")
    if command_type is None:
        return
    expected_type = CREDIT_EXTENSION_PROPOSAL_COMMAND if event.operation == "propose" else CREDIT_EXTENSION_RESOLUTION_COMMAND
    if command_type != expected_type or command_payload != credit_extension_command_payload(event):
        raise InvalidEvent("Credit extension receipt does not match the authorized command.")
