"""Command-bound, replay-checked events for private partnership self-disclosure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TYPE_CHECKING
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .components import KnowledgeRecord, RelationshipDisclosureReceipt, VisibilityScope
from .errors import InvalidEvent
from .investigation import entity_visible_to_actor
from .json_types import JsonObject
from .relationship_partnerships import RelationshipPartnership

if TYPE_CHECKING:
    from .models import WorldState


RELATIONSHIP_DISCLOSURE_RECORDED_EVENT_TYPE = "core.relationship_disclosure_recorded.v1"
DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND = "action.disclose_relationship_partnership"
_MAX_REVISION = 2**63 - 1


def relationship_disclosure_event_id(
    command_id: UUID,
    ruleset_version: str,
    source_partnership_id: UUID,
    source_revision: int,
    recipient_id: UUID,
) -> UUID:
    """Derive a stable event identity from the exact disclosure command."""

    if not all(isinstance(value, UUID) for value in (command_id, source_partnership_id, recipient_id)):
        raise ValueError("relationship disclosure event IDs require UUID command, partnership, and recipient IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("relationship disclosure event IDs require a nonempty ruleset version")
    if not isinstance(source_revision, int) or isinstance(source_revision, bool) or not 1 <= source_revision <= _MAX_REVISION:
        raise ValueError("relationship disclosure event IDs require a positive partnership revision")
    return uuid5(
        command_id,
        f"{ruleset_version}:relationship_disclosure.recorded:{source_partnership_id}:{source_revision}:{recipient_id}",
    )


def relationship_disclosure_record_id(
    command_id: UUID,
    ruleset_version: str,
    source_partnership_id: UUID,
    source_revision: int,
    recipient_id: UUID,
) -> UUID:
    """Derive the one acquired knowledge record identity for a command."""

    # Validate all inputs using the paired identity constructor.
    relationship_disclosure_event_id(
        command_id, ruleset_version, source_partnership_id, source_revision, recipient_id,
    )
    return uuid5(
        command_id,
        f"{ruleset_version}:relationship_disclosure.knowledge:{source_partnership_id}:{source_revision}:{recipient_id}",
    )


def relationship_disclosure_visibility(recipient_id: UUID) -> JsonObject:
    """Return the canonical audience for both event and recipient-held record."""

    if not isinstance(recipient_id, UUID):
        raise ValueError("relationship disclosure visibility requires a UUID recipient ID")
    return {"policy": "subject_ids", "subject_ids": [str(recipient_id)]}


def relationship_disclosure_claim(receipt: RelationshipDisclosureReceipt) -> str:
    """Fixed report of the discloser's own statement, without partner facts."""

    return f"I am in an active relationship with {receipt.opposite_participant_id}."


class RelationshipDisclosureEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class RelationshipDisclosureRecordedEvent(RelationshipDisclosureEventModel):
    """Save one recipient-held report of one partner's own words."""

    type: Literal["core.relationship_disclosure_recorded.v1"] = RELATIONSHIP_DISCLOSURE_RECORDED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    record: KnowledgeRecord
    visibility: JsonObject

    @model_validator(mode="after")
    def record_is_command_bound_and_recipient_private(self) -> RelationshipDisclosureRecordedEvent:
        receipt = self.record.relationship_disclosure
        if receipt is None:
            raise ValueError("a relationship disclosure event requires its typed receipt")
        expected_event_id = relationship_disclosure_event_id(
            self.command_id,
            self.ruleset_version,
            receipt.source_partnership_id,
            receipt.source_revision,
            receipt.recipient_id,
        )
        expected_record_id = relationship_disclosure_record_id(
            self.command_id,
            self.ruleset_version,
            receipt.source_partnership_id,
            receipt.source_revision,
            receipt.recipient_id,
        )
        if self.event_id != expected_event_id or self.record.id != expected_record_id:
            raise ValueError("relationship disclosure event and record IDs must derive from their exact command")
        if self.actor_id != receipt.discloser_id:
            raise ValueError("relationship disclosure actor must match the discloser")
        if (
            self.record.claim != relationship_disclosure_claim(receipt)
            or self.record.source_event_id != self.event_id
            or self.record.visibility != VisibilityScope(policy="subject_ids", subject_ids=[receipt.recipient_id])
        ):
            raise ValueError("relationship disclosure knowledge must preserve only the fixed private report")
        if self.visibility != relationship_disclosure_visibility(receipt.recipient_id):
            raise ValueError("relationship disclosure event must be visible only to its recipient")
        return self


RELATIONSHIP_DISCLOSURE_EVENT_ADAPTER = TypeAdapter(RelationshipDisclosureRecordedEvent)


def parse_relationship_disclosure_event(
    value: RelationshipDisclosureRecordedEvent | Mapping[str, object],
) -> RelationshipDisclosureRecordedEvent:
    """Parse the immutable, registered disclosure event."""

    return RELATIONSHIP_DISCLOSURE_EVENT_ADAPTER.validate_python(value)


def plan_relationship_disclosure(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID,
    partnership_id: UUID,
    expected_revision: int,
    recipient_id: UUID,
) -> RelationshipDisclosureRecordedEvent:
    """Plan the exact private statement receipt without changing the world."""

    _require_state(state)
    if not all(isinstance(value, UUID) for value in (world_id, branch_id, command_id, actor_id, partnership_id, recipient_id)):
        raise InvalidEvent("Relationship disclosure command IDs are invalid.")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise InvalidEvent("Relationship disclosure ruleset version is invalid.")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or not 1 <= expected_revision <= _MAX_REVISION:
        raise InvalidEvent("Relationship disclosure expected_revision is invalid.")

    partnerships = _validated_partnerships(state.relationship_partnerships)
    matches = [row for row in partnerships if row.partnership_id == partnership_id]
    if len(matches) != 1:
        raise InvalidEvent("Relationship disclosure is not available.")
    partnership = matches[0]
    # Collapse state, authority, visibility, and duplicate failures to a single
    # response so an unrelated actor cannot probe private relationship history.
    if (
        partnership.status != "active"
        or partnership.revision != expected_revision
        or partnership.updated_tick > state.current_tick
        or actor_id not in {partnership.proposer_id, partnership.partner_id}
    ):
        raise InvalidEvent("Relationship disclosure is not available.")

    opposite_id = partnership.partner_id if actor_id == partnership.proposer_id else partnership.proposer_id
    if recipient_id in {partnership.proposer_id, partnership.partner_id} or recipient_id == actor_id:
        raise InvalidEvent("Relationship disclosure is not available.")

    discloser = state.entities.get(actor_id)
    recipient = state.entities.get(recipient_id)
    location_id = None if discloser is None else discloser.location_id
    location = None if location_id is None else state.entities.get(location_id)
    if (
        discloser is None
        or discloser.kind not in {"character", "person"}
        or discloser.status != "active"
        or recipient is None
        or recipient.kind not in {"character", "person"}
        or recipient.status != "active"
        or recipient.location_id != location_id
        or location is None
        or location.kind not in {"place", "location"}
        or not entity_visible_to_actor(state, actor_id, recipient_id)
        or not entity_visible_to_actor(state, actor_id, location.id)
    ):
        raise InvalidEvent("Relationship disclosure is not available.")

    if any(
        (receipt := record.relationship_disclosure) is not None
        and receipt.source_partnership_id == partnership_id
        and receipt.source_revision == expected_revision
        and receipt.discloser_id == actor_id
        and receipt.recipient_id == recipient_id
        for record in state.knowledge
    ):
        raise InvalidEvent("Relationship disclosure is not available.")

    receipt = RelationshipDisclosureReceipt(
        source_partnership_id=partnership_id,
        source_revision=expected_revision,
        discloser_id=actor_id,
        opposite_participant_id=opposite_id,
        recipient_id=recipient_id,
        location_id=location.id,
        disclosed_tick=state.current_tick,
    )
    event_id = relationship_disclosure_event_id(
        command_id, ruleset_version, partnership_id, expected_revision, recipient_id,
    )
    record = KnowledgeRecord(
        id=relationship_disclosure_record_id(
            command_id, ruleset_version, partnership_id, expected_revision, recipient_id,
        ),
        holder_id=recipient_id,
        subject_id=actor_id,
        claim=relationship_disclosure_claim(receipt),
        source_event_id=event_id,
        received_tick=state.current_tick,
        valid_from_tick=state.current_tick,
        kind="claim",
        relationship_disclosure=receipt,
        visibility=VisibilityScope(policy="subject_ids", subject_ids=[recipient_id]),
    )
    return RelationshipDisclosureRecordedEvent(
        event_id=event_id,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
        record=record,
        visibility=relationship_disclosure_visibility(recipient_id),
    )


def validate_relationship_disclosure_batch(
    events: Sequence[object],
    *,
    state: WorldState,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    """Bind one disclosure event to the exact command and pre-event state."""

    event = next((item for item in events if isinstance(item, RelationshipDisclosureRecordedEvent)), None)
    if event is None:
        if command_type == DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND:
            raise InvalidEvent("A relationship disclosure command requires its typed event.")
        return

    if len(events) != 1 or (
        event.world_id,
        event.branch_id,
        event.command_id,
        event.actor_id,
        event.ruleset_version,
    ) != (world_id, branch_id, command_id, actor_id, ruleset_version):
        raise InvalidEvent("A relationship disclosure must be a standalone event scoped to one exact command.")

    receipt = event.record.relationship_disclosure
    if receipt is None:
        raise InvalidEvent("A relationship disclosure event requires its typed receipt.")
    if command_type is not None:
        expected_payload = {
            "kind": "disclose_relationship_partnership",
            "partnership_id": str(receipt.source_partnership_id),
            "expected_revision": receipt.source_revision,
            "recipient_id": str(receipt.recipient_id),
        }
        if command_type != DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND or command_payload != expected_payload:
            raise InvalidEvent("The relationship disclosure event does not match its command proposal.")

    planned = plan_relationship_disclosure(
        state,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        partnership_id=receipt.source_partnership_id,
        expected_revision=receipt.source_revision,
        recipient_id=receipt.recipient_id,
    )
    if event != planned:
        raise InvalidEvent("The relationship disclosure differs from its authoritative source, tick, or audience.")


def _validated_partnerships(values: object) -> list[RelationshipPartnership]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise InvalidEvent("Relationship partnerships are invalid.")
    try:
        records = [
            RelationshipPartnership.model_validate(
                item.model_dump(mode="python") if isinstance(item, RelationshipPartnership) else item,
            )
            for item in values
        ]
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Relationship partnerships are invalid.") from exc
    identifiers = [item.partnership_id for item in records]
    if len(identifiers) != len(set(identifiers)):
        raise InvalidEvent("Relationship partnerships are invalid.")
    return records


def _require_state(state: WorldState) -> None:
    from .models import WorldState as WorldStateModel

    if not isinstance(state, WorldStateModel):
        raise InvalidEvent("A validated WorldState is required.")
