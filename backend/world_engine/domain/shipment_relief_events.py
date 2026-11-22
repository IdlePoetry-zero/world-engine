"""Private, command-bound event contract for shipment relief distribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .errors import InvalidEvent
from .json_types import JsonObject
from .shipment_relief import ShipmentReliefPlan


SHIPMENT_RELIEF_DISTRIBUTED_EVENT_TYPE = "core.shipment_relief_distributed.v1"
_DISTRIBUTE_COMMAND = "action.distribute_shipment_relief"


def shipment_relief_event_id(
    command_id: UUID,
    ruleset_version: str,
    shipment_id: UUID,
    recipient_id: UUID,
) -> UUID:
    """Derive the stable event identity from command, shipment, and recipient."""

    if not isinstance(command_id, UUID) or not isinstance(shipment_id, UUID) or not isinstance(recipient_id, UUID):
        raise ValueError("shipment relief event IDs require UUID command, shipment, and recipient IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("shipment relief event IDs require a nonempty ruleset version")
    return uuid5(command_id, f"{ruleset_version}:shipment_relief.distributed:{shipment_id}:{recipient_id}")


def shipment_relief_visibility(actor_id: UUID, recipient_id: UUID) -> JsonObject:
    """Return the canonical, participant-private audience for a relief transfer."""

    if not isinstance(actor_id, UUID) or not isinstance(recipient_id, UUID):
        raise ValueError("shipment relief visibility requires UUID actor and recipient IDs")
    return {"policy": "subject_ids", "subject_ids": sorted({str(actor_id), str(recipient_id)})}


def _canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("shipment relief quantity must be a finite Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


class ShipmentReliefEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class ShipmentReliefDistributedEvent(ShipmentReliefEventModel):
    """Commit one exact distribution of delivered shipment food to a resident."""

    type: Literal["core.shipment_relief_distributed.v1"] = SHIPMENT_RELIEF_DISTRIBUTED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    plan: ShipmentReliefPlan
    visibility: JsonObject

    @model_validator(mode="after")
    def relief_event_is_command_bound_and_private(self) -> ShipmentReliefDistributedEvent:
        if self.actor_id != self.plan.actor_id:
            raise ValueError("shipment relief actor must match the event actor")
        if self.event_id != shipment_relief_event_id(
            self.command_id,
            self.ruleset_version,
            self.plan.shipment_id,
            self.plan.recipient_id,
        ):
            raise ValueError("shipment relief event ID must be bound to its command, shipment, and recipient")
        if self.visibility != shipment_relief_visibility(self.actor_id, self.plan.recipient_id):
            raise ValueError("shipment relief details must be visible only to its actor and recipient")
        return self


SHIPMENT_RELIEF_EVENT_ADAPTER = TypeAdapter(ShipmentReliefDistributedEvent)


def parse_shipment_relief_event(
    value: ShipmentReliefDistributedEvent | dict[str, object],
) -> ShipmentReliefDistributedEvent:
    """Parse a typed shipment-relief event."""

    return SHIPMENT_RELIEF_EVENT_ADAPTER.validate_python(value)


def validate_shipment_relief_batch(
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
    """Bind one standalone relief event to the exact command and scope."""

    event = next((item for item in events if isinstance(item, ShipmentReliefDistributedEvent)), None)
    if event is None:
        if command_type == _DISTRIBUTE_COMMAND:
            raise InvalidEvent("A shipment relief command requires its typed event.")
        return

    if len(events) != 1 or (
        event.world_id,
        event.branch_id,
        event.command_id,
        event.actor_id,
        event.ruleset_version,
    ) != (world_id, branch_id, command_id, actor_id, ruleset_version):
        raise InvalidEvent("Shipment relief must be a standalone event scoped to one exact command.")

    if command_type is None:
        return

    plan = event.plan
    expected_payload = {
        "kind": "distribute_shipment_relief",
        "organization_id": str(plan.organization_id),
        "shipment_id": str(plan.shipment_id),
        "recipient_id": str(plan.recipient_id),
        "quantity": _canonical_decimal(plan.quantity),
    }
    if command_type != _DISTRIBUTE_COMMAND or command_payload != expected_payload:
        raise InvalidEvent("The shipment relief event does not match its command proposal.")
