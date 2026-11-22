"""Pure selection of a hosted character's next routine trip."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from world_engine.domain.combat import active_combat_opponent
from world_engine.domain.components import (
    DailyRoutineComponent,
    DailyRoutineWindow,
    NeedsComponent,
    PlaceComponent,
    PlaceRoute,
    RoutineMobilityComponent,
)
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.npc_plans import NpcTimedExecution
from world_engine.rules.engine import route_access_reason

_MAX_TICK = 2**63 - 1
_CHARACTERS = {"character", "person"}
_PLACES = {"place", "location"}
RoutineDestination = Literal["primary", "fallback"]


@dataclass(frozen=True, slots=True)
class NpcRoutineCandidate:
    actor_id: UUID
    window_id: UUID
    occurrence_tick: int
    route_id: UUID
    reason: RoutineDestination


@dataclass(frozen=True, slots=True)
class _Option:
    destination: WorldEntity
    route: PlaceRoute | None
    reason: RoutineDestination

    @property
    def travel_ticks(self) -> int:
        return self.route.travel_ticks if self.route is not None else 0


def npc_routine_candidate(
    state: WorldState, *, excluded_actor_ids: Collection[UUID] = (),
) -> NpcRoutineCandidate | None:
    """Select one trip that can depart now and arrive at its fixed daily start."""
    selected = _first_trip(state, state.current_tick, state.current_tick, excluded_actor_ids)
    return selected[1] if selected is not None else None


def next_npc_routine_tick(
    state: WorldState, horizon: int, *, excluded_actor_ids: Collection[UUID] = (),
) -> int | None:
    """Find the next departure boundary without scanning intervening days."""
    if type(horizon) is not int or not state.current_tick < horizon <= _MAX_TICK:
        return None
    selected = _first_trip(state, state.current_tick + 1, horizon, excluded_actor_ids)
    return selected[0] if selected is not None else None


def _first_trip(
    state: WorldState, earliest: int, horizon: int, excluded_actor_ids: Collection[UUID],
) -> tuple[int, NpcRoutineCandidate] | None:
    excluded = set(excluded_actor_ids) | {state.player_id}
    actors = [
        actor for actor in state.entities.values()
        if actor.id not in excluded and actor.kind in _CHARACTERS and actor.status == "active"
        and actor.location_id is not None and isinstance(actor.components.get("routine_mobility"), dict)
        and actor.components["routine_mobility"].get("enabled") is True
    ]
    if not actors:
        return None

    reservations: dict[UUID, list[tuple[int, int]]] = defaultdict(list)
    for row in state.npc_attendance:
        reservations[row.actor_id].append((row.starts_at_tick, row.ends_at_tick))
    for plan in state.npc_plans:
        active = plan.active_execution
        if isinstance(active, NpcTimedExecution):
            reservations[plan.actor_id].append((active.started_tick, active.due_tick))
    for invitation in state.relationship_invitations:
        meeting = invitation.meeting
        if invitation.status == "accepted" and meeting is not None and meeting.end_tick > earliest:
            interval = (meeting.start_tick, meeting.end_tick)
            reservations[invitation.proposer_id].append(interval)
            reservations[invitation.invitee_id].append(interval)

    travelers = set()
    inbound = Counter()
    route_release_ticks: dict[UUID, list[int]] = defaultdict(list)
    prior_occurrences: dict[UUID, set[int]] = defaultdict(set)
    for journey in state.travel_journeys:
        if journey.arrival_tick >= earliest:
            prior_occurrences[journey.actor_id].add(journey.arrival_tick)
        if journey.status == "in_transit":
            travelers.add(journey.actor_id)
            if journey.hazard_tick is None:
                inbound[journey.to_place_id] += 1
            due_tick = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
            route_release_ticks[journey.route_id].append(due_tick)

    occupants = Counter(entity.location_id for entity in state.entities.values())
    places: dict[UUID, PlaceComponent] = {}
    candidates: list[tuple[int, NpcRoutineCandidate]] = []
    for actor in actors:
        if actor.id in travelers or active_combat_opponent(state, actor) is not None:
            continue
        try:
            routine = DailyRoutineComponent.model_validate(actor.components.get("daily_routine"))
            mobility = RoutineMobilityComponent.model_validate(actor.components["routine_mobility"])
            needs = NeedsComponent.model_validate(actor.components.get("needs", {}))
        except (TypeError, ValueError):
            continue
        origin = _place(state, actor.location_id, places)
        if origin is None:
            continue
        routes: dict[UUID, list[PlaceRoute]] = defaultdict(list)
        for route in sorted(origin.routes, key=lambda item: (item.travel_ticks, str(item.route_id))):
            if route.is_open and route.travel_ticks <= state.calendar.day_seconds:
                routes[route.to_place_id].append(route)

        commitments = sorted(reservations[actor.id])
        for window in routine.windows:
            if not _needs_allow_travel(window, needs):
                continue
            options = tuple(_options(state, actor, window, mobility, routes, places, occupants, inbound))
            if not any(option.route is not None for option in options):
                continue
            day_seconds = state.calendar.day_seconds
            for option in options:
                route = option.route
                if route is None:
                    continue
                occurrence = _next_occurrence(
                    window, day_seconds, earliest, route.travel_ticks, commitments, prior_occurrences[actor.id],
                )
                departure = occurrence - route.travel_ticks
                if departure > horizon or occurrence + window.duration_seconds > _MAX_TICK:
                    continue
                chosen = _preferred_option(
                    state, actor, window, options, occurrence, departure, commitments, route_release_ticks,
                )
                if chosen == option:
                    candidates.append((departure, NpcRoutineCandidate(
                        actor.id, window.window_id, occurrence, route.route_id, option.reason,
                    )))

    return min(candidates, key=lambda item: (
        item[0], item[1].occurrence_tick, str(item[1].actor_id), str(item[1].window_id),
    )) if candidates else None


def _place(state: WorldState, place_id: UUID | None, cache: dict[UUID, PlaceComponent]) -> PlaceComponent | None:
    if place_id is None:
        return None
    if place_id in cache:
        return cache[place_id]
    entity = state.entities.get(place_id)
    if entity is None or entity.kind not in _PLACES or entity.status != "active":
        return None
    try:
        cache[place_id] = PlaceComponent.model_validate(entity.components.get("place", {}))
    except (TypeError, ValueError):
        return None
    return cache[place_id]


def _needs_allow_travel(window: DailyRoutineWindow, needs: NeedsComponent) -> bool:
    if any(need is not None and need.value <= need.critical_below for need in (needs.food, needs.water)):
        return False
    return window.activity == "rest" or needs.rest is None or needs.rest.value > needs.rest.critical_below


def _options(
    state: WorldState, actor: WorldEntity, window: DailyRoutineWindow, mobility: RoutineMobilityComponent,
    routes: dict[UUID, list[PlaceRoute]], places: dict[UUID, PlaceComponent],
    occupants: Counter, inbound: Counter,
) -> Iterator[_Option]:
    destinations = (window.place_id, *mobility.alternatives.get(window.window_id, ()))
    for index, place_id in enumerate(destinations):
        if place_id != actor.location_id and place_id not in routes:
            continue
        destination = _place(state, place_id, places)
        if destination is None:
            continue
        # An explicit climate at the destination overrides its regional label.
        weather = destination.environment.get("weather")
        if weather is None:
            region = _place(state, destination.region_id, places)
            weather = region.environment.get("weather") if region is not None else None
        if weather in mobility.avoid_conditions:
            continue
        reason: RoutineDestination = "primary" if index == 0 else "fallback"
        if place_id == actor.location_id:
            yield _Option(state.entities[place_id], None, reason)
            break
        if destination.capacity is not None and occupants[place_id] + inbound[place_id] >= destination.capacity:
            continue
        for route in routes[place_id]:
            yield _Option(state.entities[place_id], route, reason)


def _next_occurrence(
    window: DailyRoutineWindow, day_seconds: int, earliest: int, travel_ticks: int,
    commitments: list[tuple[int, int]], prior_occurrences: set[int],
) -> int:
    def at_or_after(departure: int) -> int:
        arrival = departure + travel_ticks
        return arrival + (window.start_second - arrival) % day_seconds

    occurrence = at_or_after(earliest)
    for start, end in commitments:
        while occurrence in prior_occurrences:
            occurrence += day_seconds
        if occurrence + window.duration_seconds <= start:
            return occurrence
        if occurrence - travel_ticks < end:
            occurrence = at_or_after(end)
    while occurrence in prior_occurrences:
        occurrence += day_seconds
    return occurrence


def _preferred_option(
    state: WorldState, actor: WorldEntity, window: DailyRoutineWindow, options: tuple[_Option, ...],
    occurrence: int, earliest: int, commitments: list[tuple[int, int]],
    route_release_ticks: dict[UUID, list[int]],
) -> _Option | None:
    for option in options:
        departure = occurrence - option.travel_ticks
        if departure < earliest or any(departure < end and start < occurrence + window.duration_seconds
                                       for start, end in commitments):
            continue
        route = option.route
        if route is None:
            return option
        if route.capacity is not None and sum(due > departure for due in route_release_ticks[route.route_id]) >= route.capacity:
            continue
        at_departure = state if departure == state.current_tick else state.model_copy(update={"current_tick": departure})
        if route_access_reason(at_departure, actor, route, option.destination) is None:
            return option
    return None
