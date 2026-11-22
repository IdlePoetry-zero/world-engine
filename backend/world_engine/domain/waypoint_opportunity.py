"""Eligibility for optional scenes at a committed itinerary waypoint."""

from .components import PlaceComponent, WaypointOpportunity
from .models import WorldState
from .travel_itinerary import TravelItinerary, TravelItineraryArrivalProof


def current_waypoint_opportunity(
    state: WorldState,
    itinerary: TravelItinerary,
    cursor: TravelItineraryArrivalProof | None,
) -> WaypointOpportunity | None:
    """Find one currently available opportunity for this exact waypoint visit."""
    if (
        cursor is None
        or cursor.leg_index >= len(itinerary.legs) - 1
        or cursor.arrived_tick != state.current_tick
        or any(record.journey_id == cursor.journey_id for record in state.waypoint_opportunity_decisions)
    ):
        return None
    actor = state.entities.get(itinerary.actor_id)
    place = state.entities.get(cursor.to_place_id)
    if (
        actor is None or actor.status != "active" or actor.location_id != cursor.to_place_id
        or place is None or place.status != "active" or place.components.get("place") is None
    ):
        return None
    elapsed_ticks = state.current_tick - itinerary.created_tick
    opportunities = PlaceComponent.model_validate(place.components["place"]).waypoint_opportunities
    return next((offer for offer in opportunities if offer.is_available(state.current_tick, elapsed_ticks)), None)
