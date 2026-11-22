"""Pure structural planning for persistent, registered-route journeys."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import PlaceComponent
from .errors import InvalidEvent

if TYPE_CHECKING:
    from .models import WorldState


TRAVEL_RESOLUTION_HANDLER_ID = "travel.journey_resolve"
TRAVEL_RESOLUTION_HANDLER_VERSION = "1.0.0"
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
TravelJourneyTick = Annotated[StrictInt, Field(ge=_MIN_TICK, le=_MAX_TICK)]
TravelJourneyStatus = Literal["in_transit", "arrived", "interrupted"]
TravelJourneyOperation = Literal["arrive", "interrupt"]
TravelHazardTiming = Literal["journey_seed_v1"]
TRAVEL_HAZARD_TIMING_V1_LABEL = "movement.route_hazard_timing.v1"


class TravelJourney(BaseModel):
    """One durable journey record, without claims about route legal eligibility."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    journey_id: UUID
    actor_id: UUID
    route_id: UUID
    from_place_id: UUID
    to_place_id: UUID
    departed_tick: TravelJourneyTick
    arrival_tick: TravelJourneyTick
    hazard_tick: TravelJourneyTick | None = None
    status: TravelJourneyStatus = "in_transit"
    resolved_tick: TravelJourneyTick | None = None
    stationary_ticks: int = Field(default=0, ge=0, le=_MAX_TICK, exclude_if=lambda value: value == 0)

    @model_validator(mode="after")
    def valid_timing_and_resolution(self) -> TravelJourney:
        if self.from_place_id == self.to_place_id:
            raise ValueError("a travel journey needs distinct endpoints")
        if self.arrival_tick - self.departed_tick <= self.stationary_ticks:
            raise ValueError("a travel journey arrival must follow its departure")
        if self.hazard_tick is not None and not self.departed_tick + self.stationary_ticks < self.hazard_tick <= self.arrival_tick:
            raise ValueError("a travel hazard tick must follow departure and not exceed arrival")
        if self.status == "in_transit":
            if self.resolved_tick is not None:
                raise ValueError("an in-transit journey cannot already be resolved")
        elif self.status == "arrived":
            if self.hazard_tick is not None or self.resolved_tick != self.arrival_tick:
                raise ValueError("an arrived journey must resolve at arrival without a declared hazard")
        elif self.status == "interrupted":
            if self.resolved_tick is None:
                raise ValueError("an interrupted journey requires its resolution tick")
            if self.hazard_tick is not None:
                if self.resolved_tick != self.hazard_tick:
                    raise ValueError("a hazard interruption must resolve at its declared hazard tick")
            elif self.resolved_tick != self.arrival_tick:
                raise ValueError("a terminal interruption without a hazard resolves at the scheduled arrival tick")
        return self


def travel_resolution_schedule_id(journey_id: UUID) -> UUID:
    """Return the stable one-shot scheduled-work ID for this journey."""

    if not isinstance(journey_id, UUID):
        raise ValueError("travel resolution schedules require a UUID journey ID")
    return uuid5(journey_id, "travel-journey-resolution")


def active_travel_journey(state: WorldState, actor_id: UUID) -> TravelJourney | None:
    return next((journey for journey in state.travel_journeys
                 if journey.actor_id == actor_id and journey.status == "in_transit"), None)


def delay_travel_journey(journey: TravelJourney, start_tick: int, end_tick: int) -> TravelJourney:
    """Keep the same remaining route and hazard distance over a stationary window."""
    due_tick = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
    duration = end_tick - start_tick
    if journey.status != "in_transit" or not journey.departed_tick <= start_tick < due_tick or duration <= 0:
        raise InvalidEvent("Only an active journey can be delayed by a current stationary window.")
    try:
        return TravelJourney.model_validate(journey.model_dump(mode="python") | {
            "arrival_tick": journey.arrival_tick + duration,
            "hazard_tick": journey.hazard_tick + duration if journey.hazard_tick is not None else None,
            "stationary_ticks": journey.stationary_ticks + duration,
        })
    except ValueError as error:
        raise InvalidEvent("The delayed journey is outside the supported clock range.") from error


def travel_hazard_offset(
    journey_id: UUID,
    route_ticks: int,
    *,
    hazard_timing: TravelHazardTiming | None = None,
) -> int:
    """Return a deterministic in-route hazard offset.

    The absent timing mode deliberately retains the historic midpoint. The
    versioned mode derives its offset only from the stable journey identity.
    """

    _require_uuid(journey_id, "journey")
    if not isinstance(route_ticks, int) or isinstance(route_ticks, bool) or route_ticks <= 0:
        raise InvalidEvent("Travel route duration must be a positive integer number of ticks.")
    if hazard_timing is None:
        return max(1, route_ticks // 2)
    if hazard_timing != "journey_seed_v1":
        raise InvalidEvent("Travel hazard timing version is unsupported.")
    latest_offset = route_ticks if route_ticks == 1 else route_ticks - 1
    timing_seed = uuid5(journey_id, TRAVEL_HAZARD_TIMING_V1_LABEL)
    return timing_seed.int % latest_offset + 1


def plan_travel_departure(
    state: WorldState,
    *,
    journey_id: UUID,
    actor_id: UUID,
    route_id: UUID,
    hazard: bool,
    hazard_timing: TravelHazardTiming | None = None,
) -> TravelJourney:
    """Plan departure over one exact open route registered at the actor's origin.

    This domain boundary proves active local presence and route structure only.
    It does not prove travel capability, route access tags, destination
    visibility, diplomacy, consent, or other rule-specific permission.
    """

    _require_uuid(journey_id, "journey")
    _require_uuid(actor_id, "actor")
    _require_uuid(route_id, "route")
    if not isinstance(hazard, bool):
        raise InvalidEvent("Travel hazard declaration must be a boolean.")
    if hazard_timing is not None and hazard_timing != "journey_seed_v1":
        raise InvalidEvent("Travel hazard timing version is unsupported.")
    if hazard_timing is not None and not hazard:
        raise InvalidEvent("Travel hazard timing can be specified only for a hazardous departure.")

    entities, current_tick, journeys, schedules = _state_parts(state)
    if any(item.journey_id == journey_id for item in journeys):
        raise InvalidEvent("Travel journey ID already exists.")
    if any(item.id == travel_resolution_schedule_id(journey_id) for item in schedules):
        raise InvalidEvent("Travel resolution schedule ID already exists.")

    actor = entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise InvalidEvent("Travel actor must be an active character.")
    if any(item.actor_id == actor_id and item.status == "in_transit" for item in journeys):
        raise InvalidEvent("An actor may have only one active travel journey.")
    if actor.location_id is None:
        raise InvalidEvent("Travel actor must be at a registered origin place.")
    origin = entities.get(actor.location_id)
    if origin is None or origin.kind not in {"place", "location"} or origin.status != "active":
        raise InvalidEvent("Travel actor must be at an active registered origin place.")
    try:
        origin_place = PlaceComponent.model_validate(origin.components.get("place"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Travel origin has no valid registered route map.") from exc
    routes = [route for route in origin_place.routes if route.route_id == route_id]
    if len(routes) != 1:
        raise InvalidEvent("Travel route is not registered exactly once at the actor's origin.")
    route = routes[0]
    if not route.is_open or route.from_place_id != origin.id:
        raise InvalidEvent("Travel requires the exact open route from the actor's origin.")
    destination = entities.get(route.to_place_id)
    if destination is None or destination.kind not in {"place", "location"} or destination.status != "active":
        raise InvalidEvent("Travel route destination must be an active registered place.")
    if not isinstance(route.travel_ticks, int) or isinstance(route.travel_ticks, bool) or route.travel_ticks <= 0:
        raise InvalidEvent("Travel route duration must be a positive integer number of ticks.")
    arrival_tick = current_tick + route.travel_ticks
    if arrival_tick > _MAX_TICK:
        raise InvalidEvent("Travel arrival exceeds the supported world clock.")
    hazard_tick = (
        current_tick + travel_hazard_offset(
            journey_id, route.travel_ticks, hazard_timing=hazard_timing,
        )
        if hazard else None
    )
    if hazard_tick is not None and hazard_tick > _MAX_TICK:
        raise InvalidEvent("Travel hazard exceeds the supported world clock.")
    try:
        return TravelJourney(
            journey_id=journey_id,
            actor_id=actor_id,
            route_id=route.route_id,
            from_place_id=route.from_place_id,
            to_place_id=route.to_place_id,
            departed_tick=current_tick,
            arrival_tick=arrival_tick,
            hazard_tick=hazard_tick,
            status="in_transit",
            resolved_tick=None,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Travel departure does not fit the registered journey contract.") from exc


def plan_travel_resolution(
    state: WorldState,
    *,
    journey_id: UUID,
    actor_id: UUID,
    operation: TravelJourneyOperation,
) -> TravelJourney:
    """Resolve one active journey only at its original scheduled boundary."""

    _require_uuid(journey_id, "journey")
    _require_uuid(actor_id, "actor")
    if operation not in {"arrive", "interrupt"}:
        raise InvalidEvent("Travel journey resolution operation is invalid.")
    entities, current_tick, journeys, schedules = _state_parts(state)
    matches = [item for item in journeys if item.journey_id == journey_id]
    if len(matches) != 1:
        raise InvalidEvent("Travel journey does not exist exactly once in this state.")
    journey = matches[0]
    if journey.actor_id != actor_id:
        raise InvalidEvent("Travel journey actor does not match the requested resolver.")
    if journey.status != "in_transit":
        raise InvalidEvent("Travel journey is already resolved.")
    actor = entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.location_id is not None:
        raise InvalidEvent("Travel journey actor must remain in transit with no local presence.")
    if actor.status not in {"active", "dead", "retired"}:
        raise InvalidEvent("Travel journey actor has an unsupported terminal status.")

    due_tick = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
    if current_tick != due_tick:
        raise InvalidEvent("Travel journey can resolve only at its exact scheduled due tick.")
    schedule_id = travel_resolution_schedule_id(journey.journey_id)
    matching_schedules = [item for item in schedules if item.id == schedule_id]
    if len(matching_schedules) != 1:
        raise InvalidEvent("Travel journey is missing its unique registered resolution schedule.")
    schedule = matching_schedules[0]
    if (
        schedule.status != "queued"
        or schedule.handler_id != TRAVEL_RESOLUTION_HANDLER_ID
        or schedule.handler_version != TRAVEL_RESOLUTION_HANDLER_VERSION
        or schedule.phase != "resolution"
        or schedule.due_tick != due_tick
        or schedule.max_runs != 1
        or schedule.repeat_ticks is not None
        or schedule.completed_runs != 0
        or schedule.payload != {"journey_id": str(journey.journey_id)}
    ):
        raise InvalidEvent("Travel journey does not match its original queued resolution schedule.")

    if operation == "arrive":
        if actor.status != "active":
            raise InvalidEvent("A terminal traveler cannot arrive as an active character.")
        if journey.hazard_tick is not None or current_tick != journey.arrival_tick:
            raise InvalidEvent("Travel arrival requires its exact arrival tick and no declared hazard.")
        destination = entities.get(journey.to_place_id)
        if destination is None or destination.kind not in {"place", "location"} or destination.status != "active":
            raise InvalidEvent("Travel arrival requires an active destination.")
        try:
            place = PlaceComponent.model_validate(destination.components.get("place", {}))
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("Travel arrival destination has an invalid place record.") from exc
        if place.capacity is not None and sum(entity.location_id == destination.id for entity in entities.values()) >= place.capacity:
            raise InvalidEvent("Travel arrival destination has reached its registered capacity.")
        return journey.model_copy(update={"status": "arrived", "resolved_tick": current_tick})

    if actor.status == "active" and journey.hazard_tick is None:
        raise InvalidEvent("An active journey may be interrupted only at a declared hazard tick.")
    return journey.model_copy(update={"status": "interrupted", "resolved_tick": current_tick})


def _state_parts(state):
    entities = getattr(state, "entities", None)
    current_tick = getattr(state, "current_tick", None)
    if not isinstance(entities, Mapping):
        raise InvalidEvent("Travel journey planning requires a valid entity registry.")
    if not isinstance(current_tick, int) or isinstance(current_tick, bool) or not _MIN_TICK <= current_tick <= _MAX_TICK:
        raise InvalidEvent("Travel journey world tick must be a signed integer.")
    try:
        raw_journeys = getattr(state, "travel_journeys")
        raw_schedules = getattr(state, "scheduled_events")
    except AttributeError as exc:
        raise InvalidEvent("Travel journey planning requires journey and schedule registries.") from exc
    if not isinstance(raw_journeys, Sequence) or isinstance(raw_journeys, (str, bytes)):
        raise InvalidEvent("Travel journey registry is invalid.")
    if not isinstance(raw_schedules, Sequence) or isinstance(raw_schedules, (str, bytes)):
        raise InvalidEvent("Travel schedule registry is invalid.")
    try:
        journeys = [
            TravelJourney.model_validate(item.model_dump(mode="python") if isinstance(item, TravelJourney) else item)
            for item in raw_journeys
        ]
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Stored travel journey record is invalid.") from exc
    return entities, current_tick, journeys, list(raw_schedules)


def _require_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise InvalidEvent(f"Travel {label} ID is invalid.")
