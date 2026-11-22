"""Private, command-bound event contract for explicit partnerships."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .errors import InvalidEvent
from .json_types import JsonObject
from .relationship_partnerships import RelationshipPartnership


RELATIONSHIP_PARTNERSHIP_CHANGED_EVENT_TYPE = "core.relationship_partnership_changed.v1"
NPC_PARTNERSHIP_RESPONSE_COMMAND = "action.request_relationship_partnership_response"
_PROPOSE_COMMAND = "action.propose_relationship_partnership"
_RESOLVE_COMMAND = "action.resolve_relationship_partnership"
_UPDATE_COMMAND = "action.update_relationship_partnership_proposal"
_MAX_REVISION = 2**63 - 1
PartnershipOperation = Literal["propose", "revise", "counter", "accept", "decline", "withdraw", "end"]


def partnership_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: PartnershipOperation,
    partnership_id: UUID,
) -> UUID:
    """Derive a stable event identity from command, operation, and partnership."""

    if not isinstance(command_id, UUID) or not isinstance(partnership_id, UUID):
        raise ValueError("partnership event IDs require UUID command and partnership IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("partnership event IDs require a nonempty ruleset version")
    if operation not in {"propose", "revise", "counter", "accept", "decline", "withdraw", "end"}:
        raise ValueError("partnership operation is unsupported")
    return uuid5(command_id, f"{ruleset_version}:relationship_partnership.{operation}:{partnership_id}")


def partnership_visibility(proposer_id: UUID, partner_id: UUID) -> JsonObject:
    """Return the canonical, two-participant-only audience for a partnership."""

    if not isinstance(proposer_id, UUID) or not isinstance(partner_id, UUID):
        raise ValueError("partnership visibility requires UUID participant IDs")
    if proposer_id == partner_id:
        raise ValueError("partnership visibility requires distinct participants")
    return {"policy": "subject_ids", "subject_ids": sorted((str(proposer_id), str(partner_id)))}


class PartnershipEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class RelationshipPartnershipChangedEvent(PartnershipEventModel):
    """Commit one explicit proposal, response, withdrawal, or ending."""

    type: Literal["core.relationship_partnership_changed.v1"] = RELATIONSHIP_PARTNERSHIP_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    ruleset_version: str = Field(min_length=1, max_length=100)
    operation: PartnershipOperation
    expected_revision: StrictInt = Field(ge=0, le=_MAX_REVISION)
    resulting_partnership: RelationshipPartnership
    visibility: JsonObject

    @model_validator(mode="after")
    def transition_is_exact_and_participant_private(self) -> RelationshipPartnershipChangedEvent:
        partnership = self.resulting_partnership
        if self.event_id != partnership_event_id(
            self.command_id, self.ruleset_version, self.operation, partnership.partnership_id,
        ):
            raise ValueError("partnership event ID must be bound to its command, operation, and partnership")

        if self.operation == "propose":
            if partnership.partnership_id != uuid5(self.command_id, "relationship-partnership"):
                raise ValueError("new partnership ID must derive from its proposal command")
            if self.expected_revision != 0:
                raise ValueError("a new partnership proposal must expect revision zero")
            if partnership.proposal_author_id is not None:
                raise ValueError("an original partnership proposal must use the legacy original-author marker")
            expected_status = "pending"
            expected_actor = partnership.proposer_id
            expected_result_revision = 1
        elif self.operation in {"revise", "counter"}:
            if self.expected_revision < 1:
                raise ValueError("a partnership update must expect an existing positive revision")
            if partnership.proposal_author_id is None or partnership.proposal_author_id != self.actor_id:
                raise ValueError("a revised or countered proposal must identify its new author")
            if not partnership.proposal_terms:
                raise ValueError("a revised or countered proposal must carry full nonempty terms")
            expected_actor = partnership.current_proposal_author_id
            expected_status = "pending"
            expected_result_revision = self.expected_revision + 1
        else:
            if self.expected_revision < 1:
                raise ValueError("a partnership transition must expect an existing positive revision")
            expected_actor = (
                partnership.current_proposal_recipient_id
                if self.operation in {"accept", "decline"}
                else partnership.current_proposal_author_id
                if self.operation == "withdraw"
                else self.actor_id
            )
            expected_status = {
                "accept": "active",
                "decline": "declined",
                "withdraw": "withdrawn",
                "end": "ended",
            }[self.operation]
            expected_result_revision = self.expected_revision + 1

        if partnership.status != expected_status or partnership.revision != expected_result_revision:
            raise ValueError("partnership result status and revision must match the requested operation")
        if self.requested_by is not None:
            if self.operation in {"accept", "decline"}:
                if self.requested_by != partnership.current_proposal_author_id:
                    raise ValueError("a requested partnership response must be requested by the current author")
                if self.actor_id != partnership.current_proposal_recipient_id:
                    raise ValueError("the current proposal recipient must be the event actor")
            elif self.operation == "counter":
                if self.actor_id != partnership.current_proposal_author_id:
                    raise ValueError("a hosted counterproposal actor must become the current author")
                if self.requested_by != partnership.current_proposal_recipient_id:
                    raise ValueError("a hosted counterproposal must be requested by its current recipient")
            else:
                raise ValueError("only a partner response or counterproposal may be requested from a participant")
        if self.operation == "end" and self.actor_id not in {partnership.proposer_id, partnership.partner_id}:
            raise ValueError("only a partnership participant may end the partnership")
        if self.actor_id != expected_actor:
            raise ValueError("partnership operation actor does not have authority for this transition")
        if self.visibility != partnership_visibility(partnership.proposer_id, partnership.partner_id):
            raise ValueError("partnership details must be visible only to its two participants")
        return self


RELATIONSHIP_PARTNERSHIP_EVENT_ADAPTER = TypeAdapter(RelationshipPartnershipChangedEvent)


def parse_relationship_partnership_event(
    value: RelationshipPartnershipChangedEvent | dict[str, object],
) -> RelationshipPartnershipChangedEvent:
    """Parse the typed relationship-partnership event."""

    return RELATIONSHIP_PARTNERSHIP_EVENT_ADAPTER.validate_python(value)


def validate_relationship_partnership_batch(
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    """Bind a standalone partnership event to its exact command and scope."""

    event = next((item for item in events if isinstance(item, RelationshipPartnershipChangedEvent)), None)
    if event is None:
        if command_type in {_PROPOSE_COMMAND, _RESOLVE_COMMAND, _UPDATE_COMMAND, NPC_PARTNERSHIP_RESPONSE_COMMAND}:
            raise InvalidEvent("A relationship partnership command requires its typed event.")
        return

    command_actor_id = event.requested_by or event.actor_id
    if len(events) != 1 or (
        event.world_id,
        event.branch_id,
        event.command_id,
        command_actor_id,
        event.ruleset_version,
    ) != (world_id, branch_id, command_id, actor_id, ruleset_version):
        raise InvalidEvent("A relationship partnership must be a standalone event scoped to one exact command.")

    if command_type is None:
        return

    partnership = event.resulting_partnership
    if event.requested_by is not None:
        expected_command = NPC_PARTNERSHIP_RESPONSE_COMMAND
        expected_payload = {
            "kind": "request_relationship_partnership_response",
            "partnership_id": str(partnership.partnership_id),
            "expected_revision": event.expected_revision,
        }
    elif event.operation == "propose":
        expected_command = _PROPOSE_COMMAND
        expected_payload = {
            "kind": "propose_relationship_partnership",
            "partner_id": str(partnership.partner_id),
        }
        if partnership.proposal_terms:
            expected_payload["proposal_terms"] = list(partnership.proposal_terms)
    elif event.operation in {"revise", "counter"}:
        expected_command = _UPDATE_COMMAND
        expected_payload = {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(partnership.partnership_id),
            "expected_revision": event.expected_revision,
            "operation": event.operation,
            "proposal_terms": list(partnership.proposal_terms),
        }
    else:
        expected_command = _RESOLVE_COMMAND
        expected_payload = {
            "kind": "resolve_relationship_partnership",
            "partnership_id": str(partnership.partnership_id),
            "operation": event.operation,
            "expected_revision": event.expected_revision,
        }
    if (
        command_type != expected_command
        or command_payload != expected_payload
        or event.operation != "propose" and type(command_payload.get("expected_revision")) is not int
    ):
        raise InvalidEvent("The relationship partnership event does not match its command proposal.")
