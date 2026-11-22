"""Command-bound event contract for persistent travel journeys."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .errors import InvalidEvent
from .json_types import JsonObject
from .travel_journeys import TravelHazardTiming, TravelJourney, TravelJourneyTick


TRAVEL_JOURNEY_CHANGED_EVENT_TYPE = "core.travel_journey_changed.v1"
TravelJourneyOperation = Literal["depart", "arrive", "interrupt", "delay"]


def travel_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: TravelJourneyOperation,
    journey_id: UUID,
) -> UUID:
    """Derive a stable event identity from the command and journey transition."""

    if not isinstance(command_id, UUID) or not isinstance(journey_id, UUID):
        raise ValueError("travel event IDs require UUID command and journey IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("travel event IDs require a nonempty ruleset version")
    if operation not in {"depart", "arrive", "interrupt", "delay"}:
        raise ValueError("travel journey operation is unsupported")
    return uuid5(command_id, f"{ruleset_version}:travel_journey.{operation}:{journey_id}")


def travel_journey_visibility(actor_id: UUID) -> JsonObject:
    """Return the focal actor-only audience for a journey transition."""

    if not isinstance(actor_id, UUID):
        raise ValueError("travel journey visibility requires a UUID actor ID")
    return {"policy": "subject_ids", "subject_ids": [str(actor_id)]}


class TravelJourneyEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class RoutineJourneyChoice(TravelJourneyEventModel):
    window_id: UUID
    occurrence_tick: TravelJourneyTick
    reason: Literal["scheduled_destination", "weather", "destination_capacity", "route_or_timing"]


class TravelJourneyChangedEvent(TravelJourneyEventModel):
    """Record a persistent journey departure, arrival, or interruption."""

    type: Literal["core.travel_journey_changed.v1"] = TRAVEL_JOURNEY_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    operation: TravelJourneyOperation
    journey: TravelJourney
    visibility: JsonObject
    stationary_from_tick: TravelJourneyTick | None = Field(default=None, exclude_if=lambda value: value is None)
    stationary_until_tick: TravelJourneyTick | None = Field(default=None, exclude_if=lambda value: value is None)
    hazard_timing: TravelHazardTiming | None = Field(default=None, exclude_if=lambda value: value is None)
    routine_choice: RoutineJourneyChoice | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def transition_is_bound_and_private(self) -> TravelJourneyChangedEvent:
        if self.actor_id != self.journey.actor_id:
            raise ValueError("travel journey actor must match the event actor")
        if self.event_id != travel_event_id(
            self.command_id, self.ruleset_version, self.operation, self.journey.journey_id,
        ):
            raise ValueError("travel event ID must be bound to its command, operation, and journey")
        expected_status = {
            "depart": "in_transit",
            "arrive": "arrived",
            "interrupt": "interrupted",
            "delay": "in_transit",
        }[self.operation]
        if self.journey.status != expected_status:
            raise ValueError("travel journey state must match its operation")
        if self.visibility != travel_journey_visibility(self.actor_id):
            raise ValueError("travel journey details must be visible only to its actor")
        if self.hazard_timing is not None and (
            self.operation != "depart" or self.journey.hazard_tick is None
        ):
            raise ValueError("travel hazard timing is permitted only on a hazardous departure")
        if self.routine_choice is not None and (
            self.operation != "depart" or self.routine_choice.occurrence_tick != self.journey.arrival_tick
        ):
            raise ValueError("routine choice must match a departure's scheduled arrival")
        if self.operation == "delay":
            if (self.stationary_from_tick is None or self.stationary_until_tick is None
                or self.stationary_from_tick >= self.stationary_until_tick):
                raise ValueError("travel delay requires a positive stationary interval")
        elif self.stationary_from_tick is not None or self.stationary_until_tick is not None:
            raise ValueError("only travel delays can record a stationary interval")
        return self


TRAVEL_JOURNEY_EVENT_ADAPTER = TypeAdapter(TravelJourneyChangedEvent)


def parse_travel_journey_event(
    value: TravelJourneyChangedEvent | dict[str, object],
) -> TravelJourneyChangedEvent:
    """Parse a typed travel journey event."""

    return TRAVEL_JOURNEY_EVENT_ADAPTER.validate_python(value)


def validate_travel_journey_scope(
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
) -> None:
    """Validate world, branch, command, and ruleset scope for any journey events."""

    journey_events = [item for item in events if isinstance(item, TravelJourneyChangedEvent)]
    for event in journey_events:
        if (
            event.world_id,
            event.branch_id,
            event.command_id,
            event.ruleset_version,
        ) != (world_id, branch_id, command_id, ruleset_version):
            raise InvalidEvent("Travel journey event does not match its trusted command scope.")
        if event.operation == "delay":
            # Resolve the import lazily because models also registers journey events.
            from .models import TimeAdvancedEvent

            advances = [item for item in events if isinstance(item, TimeAdvancedEvent)]
            if (len(journey_events) != 1 or not events or events[0] is not event or len(advances) != 1
                or advances[0].to_tick != event.stationary_until_tick):
                raise InvalidEvent("Travel delay must precede the matching clock advance in its sole journey batch.")
