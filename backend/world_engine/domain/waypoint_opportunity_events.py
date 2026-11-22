"""Private, durable decisions about optional travel waypoint opportunities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .errors import InvalidEvent
from .json_types import JsonObject


WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE = "core.waypoint_opportunity_decision_recorded.v1"
WAYPOINT_OPPORTUNITY_COMMAND = "simulation.waypoint_opportunity"
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
WaypointOpportunityChoice = Literal["continue", "stop"]


def waypoint_opportunity_event_id(journey_id: UUID) -> UUID:
    """Return the stable event identity for the sole decision on a journey."""

    if not isinstance(journey_id, UUID):
        raise ValueError("waypoint opportunity event IDs require a UUID journey ID")
    return uuid5(journey_id, f"{WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE}:event")


def waypoint_opportunity_request_id(journey_id: UUID, opportunity_id: UUID, state_version: int) -> UUID:
    """Return the stable user-facing request identity for one journey opportunity."""

    if not isinstance(journey_id, UUID) or not isinstance(opportunity_id, UUID):
        raise ValueError("waypoint opportunity request IDs require UUID journey and opportunity IDs")
    if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
        raise ValueError("waypoint opportunity request IDs require a nonnegative state version")
    return uuid5(
        journey_id,
        f"{WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE}:request:{opportunity_id}:{state_version}",
    )


def waypoint_opportunity_visibility(actor_id: UUID) -> JsonObject:
    """Return the exact actor-only audience for a waypoint decision."""

    if not isinstance(actor_id, UUID):
        raise ValueError("waypoint opportunity visibility requires a UUID actor ID")
    return {"policy": "subject_ids", "subject_ids": [str(actor_id)]}


class _WaypointOpportunityModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class WaypointOpportunityDecision(_WaypointOpportunityModel):
    """One actor's durable choice at an arrived journey's destination."""

    journey_id: UUID
    actor_id: UUID
    opportunity_id: UUID
    state_version: StrictInt = Field(ge=0)
    at_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    itinerary_started_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    choice: WaypointOpportunityChoice

    @model_validator(mode="after")
    def itinerary_started_before_decision(self) -> WaypointOpportunityDecision:
        if self.itinerary_started_tick > self.at_tick:
            raise ValueError("a waypoint opportunity decision cannot precede its itinerary")
        return self


class WaypointOpportunityDecisionRecordedEvent(_WaypointOpportunityModel):
    """A command-scoped, actor-private record of one journey decision."""

    type: Literal["core.waypoint_opportunity_decision_recorded.v1"] = (
        WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE
    )
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    record: WaypointOpportunityDecision
    visibility: JsonObject

    @model_validator(mode="after")
    def event_is_bound_and_private(self) -> WaypointOpportunityDecisionRecordedEvent:
        if self.actor_id != self.record.actor_id:
            raise ValueError("waypoint opportunity actor must match the event actor")
        if self.event_id != waypoint_opportunity_event_id(self.record.journey_id):
            raise ValueError("waypoint opportunity event ID must be bound to its journey")
        if self.visibility != waypoint_opportunity_visibility(self.actor_id):
            raise ValueError("waypoint opportunity decision must be visible only to its actor")
        return self


WAYPOINT_OPPORTUNITY_EVENT_ADAPTER = TypeAdapter(WaypointOpportunityDecisionRecordedEvent)


def parse_waypoint_opportunity_event(
    value: WaypointOpportunityDecisionRecordedEvent | dict[str, object],
) -> WaypointOpportunityDecisionRecordedEvent:
    """Parse a typed waypoint opportunity event."""

    return WAYPOINT_OPPORTUNITY_EVENT_ADAPTER.validate_python(value)


def validate_waypoint_opportunity_batch(
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
    state_version: int | None = None,
) -> None:
    """Bind a decision to its sole command, trusted scope, and exact payload."""

    decisions = [event for event in events if isinstance(event, WaypointOpportunityDecisionRecordedEvent)]
    if not decisions:
        if command_type == WAYPOINT_OPPORTUNITY_COMMAND:
            raise InvalidEvent("A waypoint opportunity command requires its typed event.")
        return

    if len(events) != 1 or len(decisions) != 1:
        raise InvalidEvent("A waypoint opportunity decision must be a standalone event.")
    event = decisions[0]
    record = event.record
    if (
        event.event_id != waypoint_opportunity_event_id(record.journey_id)
        or (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version)
        != (world_id, branch_id, command_id, actor_id, ruleset_version)
        or event.actor_id != record.actor_id
        or event.visibility != waypoint_opportunity_visibility(event.actor_id)
        or (state_version is not None and record.state_version != state_version)
    ):
        raise InvalidEvent("A waypoint opportunity decision must match its trusted command scope and private actor.")
    if (command_type is None) != (command_payload is None):
        raise InvalidEvent("Waypoint opportunity command type and payload must be supplied together.")
    if command_type is None:
        return
    if command_type != WAYPOINT_OPPORTUNITY_COMMAND:
        raise InvalidEvent("A waypoint opportunity decision requires its registered simulation command.")
    expected_payload = {
        "request_id": str(waypoint_opportunity_request_id(
            record.journey_id, record.opportunity_id, record.state_version,
        )),
        "option_id": record.choice,
    }
    if command_payload != expected_payload:
        raise InvalidEvent("A waypoint opportunity decision must match its exact request and option payload.")
