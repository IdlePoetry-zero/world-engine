"""Derive the same routine departure in planning and under the storage lease."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID, uuid5

from world_engine.domain.models import WorldState
from world_engine.domain.travel_journey_events import RoutineJourneyChoice, TravelJourneyChangedEvent
from world_engine.rules.contracts import RuleContext, RuleResolution
from world_engine.rules.engine import RuleEngine


def plan_npc_routine_departure(
    state: WorldState,
    context: RuleContext,
    *,
    excluded_actor_ids: Collection[UUID],
    rules: RuleEngine,
) -> RuleResolution | None:
    from .npc_routine_schedule import npc_routine_candidate

    candidate = npc_routine_candidate(state, excluded_actor_ids=excluded_actor_ids)
    if candidate is None:
        return None
    journey_id = uuid5(
        context.world_id,
        f"npc-routine.v1:{candidate.actor_id}:{candidate.window_id}:{candidate.occurrence_tick}",
    )
    if any(journey.journey_id == journey_id for journey in state.travel_journeys):
        return None
    result = rules.evaluate_travel_departure(
        state, candidate.route_id, context.model_copy(update={"actor_id": candidate.actor_id}),
        itinerary_journey_id=journey_id,
    )
    if result.outcome.status != "succeeded":
        return None
    departure, = result.events
    if not isinstance(departure, TravelJourneyChangedEvent):
        raise RuntimeError("A routine departure must use the registered journey event.")
    choice = RoutineJourneyChoice(window_id=candidate.window_id, occurrence_tick=candidate.occurrence_tick,
                                 reason=candidate.reason_code)
    return result.model_copy(update={"events": [departure.model_copy(update={"routine_choice": choice})]})
