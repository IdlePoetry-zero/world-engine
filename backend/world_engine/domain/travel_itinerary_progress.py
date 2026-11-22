"""Pure durable-job helpers for ordered travel itineraries.

The durable job owns its cursor. World state remains authoritative for each
committed journey, and only a typed arrival event matched to the prior
in-transit state can advance that cursor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldState
from world_engine.domain.travel_itinerary import (
    TravelItinerary,
    TravelItineraryArrivalProof,
    TravelItineraryLeg,
    TravelItineraryLegRevalidation,
    revalidate_travel_itinerary_leg,
    travel_itinerary_journey_id,
)
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)


TravelItineraryJobStepOutcome = Literal[
    "ready_to_run",
    "resume_in_transit",
    "blocked_interrupted",
    "completed",
]

_ITINERARY_REPLAN_REASONS = frozenset({
    "route_hazard",
    "travel_journey_interrupted",
    "deadline_passed",
    "remaining_route_cannot_fit_deadline",
})


def itinerary_requires_replan(reason_code: object) -> bool:
    """Whether a durable interruption ended the current itinerary's authority.

    Route hazards and deadline exhaustion cannot retry the same stable journey
    identity or immutable window. Transient route revalidation failures remain
    recoverable if conditions change before the player resumes.
    """

    return type(reason_code) is str and reason_code in _ITINERARY_REPLAN_REASONS


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class TravelItineraryJobStep(_FrozenModel):
    """One pure view of the itinerary job at its durable cursor."""

    itinerary_id: UUID
    outcome: TravelItineraryJobStepOutcome
    leg_index: int | None = Field(default=None, ge=0)
    leg: TravelItineraryLeg | None = None
    journey_id: UUID | None = None
    journey: TravelJourney | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=120)
    revalidation: TravelItineraryLegRevalidation | None = None

    @model_validator(mode="after")
    def step_shape_matches_outcome(self) -> TravelItineraryJobStep:
        if self.outcome == "completed":
            if any(value is not None for value in (
                self.leg_index, self.leg, self.journey_id, self.journey,
                self.reason_code, self.revalidation,
            )):
                raise ValueError("a completed itinerary has no active leg or interruption")
            return self
        if self.leg_index is None or self.leg is None or self.journey_id is None:
            raise ValueError("an active itinerary step requires its fixed leg and journey ID")
        if self.leg.leg_index != self.leg_index:
            raise ValueError("itinerary job step index must match its immutable leg")
        if self.outcome == "ready_to_run":
            if self.journey is not None or self.reason_code is not None or self.revalidation is None:
                raise ValueError("a ready itinerary step requires only a current route revalidation")
            if self.revalidation.outcome != "current":
                raise ValueError("a ready itinerary step requires a current route snapshot")
        elif self.outcome == "resume_in_transit":
            if self.journey is None or self.reason_code is not None or self.revalidation is not None:
                raise ValueError("an in-transit step requires its persisted journey")
        elif self.outcome == "blocked_interrupted":
            if self.reason_code is None:
                raise ValueError("a blocked itinerary step requires a reason")
        return self


def inspect_travel_itinerary_job(
    state: WorldState,
    itinerary: TravelItinerary,
    *,
    cursor: TravelItineraryArrivalProof | None,
) -> TravelItineraryJobStep:
    """Compute the next fixed itinerary leg from durable cursor and trusted state.

    A missing cursor selects leg zero. A supplied cursor must be the exact
    arrival proof for the immediately preceding leg, with the matching arrived
    journey in ``state``. The cursor is never inferred from the actor's current
    location.
    """

    plan = _validated_itinerary(itinerary)
    rows = _journey_rows(state)
    by_id = {journey.journey_id: journey for journey in rows}

    typed_cursor = _validated_cursor(cursor)
    if typed_cursor is None:
        leg_index = 0
    else:
        _check_cursor_and_prior_arrivals(state, plan, typed_cursor, by_id)
        if typed_cursor.leg_index == len(plan.legs) - 1:
            return TravelItineraryJobStep(itinerary_id=plan.itinerary_id, outcome="completed")
        leg_index = typed_cursor.leg_index + 1

    leg = plan.legs[leg_index]
    expected_journey_id = travel_itinerary_journey_id(plan.itinerary_id, leg.leg_id)
    actor = state.entities.get(plan.actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise InvalidEvent("A running itinerary cannot continue with a missing or terminal actor.")

    # All persisted plan legs before the cursor must already be exact arrivals;
    # any future-leg record means the cursor or event order is corrupted.
    for prior_index in range(leg_index):
        prior_leg = plan.legs[prior_index]
        prior_id = travel_itinerary_journey_id(plan.itinerary_id, prior_leg.leg_id)
        prior = by_id.get(prior_id)
        if prior is None:
            raise InvalidEvent("An itinerary cursor skips a leg without its committed arrival journey.")
        _validate_journey_for_leg(prior, plan, prior_leg)
        if prior.status != "arrived":
            raise InvalidEvent("An itinerary cursor cannot pass a leg without an arrival.")
    for future_leg in plan.legs[leg_index + 1 :]:
        future_id = travel_itinerary_journey_id(plan.itinerary_id, future_leg.leg_id)
        if future_id in by_id:
            raise InvalidEvent("A later itinerary leg has a journey before its preceding arrival.")

    current_journey = by_id.get(expected_journey_id)
    active_for_actor = [
        journey for journey in rows
        if journey.actor_id == plan.actor_id and journey.status == "in_transit"
    ]
    if len(active_for_actor) > 1:
        raise InvalidEvent("An itinerary actor has multiple active travel journeys.")
    if active_for_actor and active_for_actor[0].journey_id != expected_journey_id:
        raise InvalidEvent("The itinerary actor has an unexpected active travel journey.")

    if current_journey is not None:
        _validate_journey_for_leg(current_journey, plan, leg)
        if current_journey.status == "in_transit":
            if actor.location_id is not None:
                raise InvalidEvent("An in-transit itinerary actor has local presence.")
            _validate_queued_resolution(state, current_journey)
            return TravelItineraryJobStep(
                itinerary_id=plan.itinerary_id,
                outcome="resume_in_transit",
                leg_index=leg_index,
                leg=leg,
                journey_id=expected_journey_id,
                journey=current_journey,
            )
        if current_journey.status == "interrupted":
            return TravelItineraryJobStep(
                itinerary_id=plan.itinerary_id,
                outcome="blocked_interrupted",
                leg_index=leg_index,
                leg=leg,
                journey_id=expected_journey_id,
                journey=current_journey,
                reason_code="travel_journey_interrupted",
            )
        raise InvalidEvent("An arrived itinerary leg is missing its durable cursor proof.")

    if active_for_actor:
        raise InvalidEvent("An itinerary actor has an active journey without the expected leg record.")
    if actor.location_id is None:
        raise InvalidEvent("An itinerary actor has no location but no matching in-transit journey.")

    revalidation = revalidate_travel_itinerary_leg(
        state,
        plan,
        leg_index,
        previous_arrival=typed_cursor,
    )
    if revalidation.outcome == "current":
        return TravelItineraryJobStep(
            itinerary_id=plan.itinerary_id,
            outcome="ready_to_run",
            leg_index=leg_index,
            leg=leg,
            journey_id=expected_journey_id,
            revalidation=revalidation,
        )
    if revalidation.outcome in {
        "route_invalidated", "route_unobserved", "actor_not_at_origin",
        "deadline_elapsed", "deadline_unreachable",
    }:
        return TravelItineraryJobStep(
            itinerary_id=plan.itinerary_id,
            outcome="blocked_interrupted",
            leg_index=leg_index,
            leg=leg,
            journey_id=expected_journey_id,
            reason_code=revalidation.reason,
            revalidation=revalidation,
        )
    raise InvalidEvent(
        f"Itinerary revalidation returned a corrupt cursor or actor state: {revalidation.outcome}."
    )


def build_travel_itinerary_arrival_proof(
    itinerary: TravelItinerary,
    *,
    leg_index: int,
    prior_state: WorldState,
    arrival_event: TravelJourneyChangedEvent,
    world_id: UUID,
    branch_id: UUID,
    cursor: TravelItineraryArrivalProof | None,
) -> TravelItineraryArrivalProof:
    """Build the next cursor only from the exact typed arrival transition.

    ``prior_state`` must be the trusted state immediately before applying the
    arrival event. The durable worker must have committed this leg's stable
    journey ID at departure and must advance its cursor atomically with the
    arrival commit.
    """

    plan = _validated_itinerary(itinerary)
    if type(leg_index) is not int or not 0 <= leg_index < len(plan.legs):
        raise InvalidEvent("Arrival proof leg index is outside the itinerary.")
    if not isinstance(world_id, UUID) or not isinstance(branch_id, UUID):
        raise InvalidEvent("Arrival proof validation requires trusted world and branch IDs.")
    if not isinstance(arrival_event, TravelJourneyChangedEvent):
        raise InvalidEvent("An itinerary cursor requires a typed travel arrival event.")
    try:
        event = TravelJourneyChangedEvent.model_validate(arrival_event.model_dump(mode="python"))
    except (TypeError, ValueError) as error:
        raise InvalidEvent("The travel arrival event is malformed or not command-bound.") from error
    if event.operation != "arrive":
        raise InvalidEvent("Only an arrival event can advance the itinerary cursor.")
    if event.world_id != world_id or event.branch_id != branch_id:
        raise InvalidEvent("Travel arrival event belongs to another world or branch.")

    step = inspect_travel_itinerary_job(prior_state, plan, cursor=cursor)
    if (
        step.outcome != "resume_in_transit"
        or step.leg_index != leg_index
        or step.journey is None
    ):
        raise InvalidEvent("Arrival event is out of order for the itinerary's durable cursor.")
    departed = step.journey
    expected_arrived = departed.model_copy(update={
        "status": "arrived",
        "resolved_tick": departed.arrival_tick,
    })
    if (
        prior_state.current_tick != departed.arrival_tick
        or departed.hazard_tick is not None
        or event.actor_id != plan.actor_id
        or event.journey.journey_id != step.journey_id
        or event.journey != expected_arrived
    ):
        raise InvalidEvent("Travel arrival does not match the exact in-transit itinerary leg and tick.")

    return TravelItineraryArrivalProof(
        itinerary_id=plan.itinerary_id,
        leg_id=plan.legs[leg_index].leg_id,
        leg_index=leg_index,
        journey_id=departed.journey_id,
        actor_id=plan.actor_id,
        route_id=plan.legs[leg_index].route_id,
        from_place_id=plan.legs[leg_index].from_place_id,
        to_place_id=plan.legs[leg_index].to_place_id,
        route_fingerprint=plan.legs[leg_index].route_fingerprint,
        departed_tick=departed.departed_tick,
        arrived_tick=event.journey.resolved_tick,
    )


def _validated_itinerary(itinerary: TravelItinerary) -> TravelItinerary:
    if not isinstance(itinerary, TravelItinerary):
        raise InvalidEvent("Travel itinerary job requires an immutable typed itinerary.")
    try:
        return TravelItinerary.model_validate(itinerary.model_dump(mode="python"))
    except (TypeError, ValueError) as error:
        raise InvalidEvent("Stored itinerary plan is invalid.") from error


def _validated_cursor(cursor: TravelItineraryArrivalProof | None) -> TravelItineraryArrivalProof | None:
    if cursor is None:
        return None
    if not isinstance(cursor, TravelItineraryArrivalProof):
        raise InvalidEvent("Travel itinerary cursor must be a typed arrival proof.")
    try:
        return TravelItineraryArrivalProof.model_validate(cursor.model_dump(mode="python"))
    except (TypeError, ValueError) as error:
        raise InvalidEvent("Travel itinerary arrival cursor is invalid.") from error


def _journey_rows(state: WorldState) -> list[TravelJourney]:
    raw = getattr(state, "travel_journeys", None)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise InvalidEvent("Travel itinerary job requires the trusted journey registry.")
    rows: list[TravelJourney] = []
    seen: set[UUID] = set()
    for item in raw:
        try:
            journey = TravelJourney.model_validate(
                item.model_dump(mode="python") if isinstance(item, TravelJourney) else item,
            )
        except (TypeError, ValueError) as error:
            raise InvalidEvent("Stored journey registry contains an invalid record.") from error
        if journey.journey_id in seen:
            raise InvalidEvent("Stored journey registry repeats a journey ID.")
        seen.add(journey.journey_id)
        rows.append(journey)
    return rows


def _validate_journey_for_leg(
    journey: TravelJourney,
    itinerary: TravelItinerary,
    leg: TravelItineraryLeg,
) -> None:
    expected_id = travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id)
    if (
        journey.journey_id != expected_id
        or journey.actor_id != itinerary.actor_id
        or journey.route_id != leg.route_id
        or journey.from_place_id != leg.from_place_id
        or journey.to_place_id != leg.to_place_id
        or journey.arrival_tick - journey.departed_tick != leg.travel_ticks + journey.stationary_ticks
    ):
        raise InvalidEvent("Stored journey does not match its fixed itinerary leg identity and route.")


def _check_cursor_and_prior_arrivals(
    state: WorldState,
    itinerary: TravelItinerary,
    cursor: TravelItineraryArrivalProof,
    journeys_by_id: Mapping[UUID, TravelJourney],
) -> None:
    if cursor.itinerary_id != itinerary.itinerary_id or cursor.actor_id != itinerary.actor_id:
        raise InvalidEvent("Travel itinerary cursor belongs to another plan or actor.")
    if not 0 <= cursor.leg_index < len(itinerary.legs):
        raise InvalidEvent("Travel itinerary cursor index is outside the ordered legs.")
    leg = itinerary.legs[cursor.leg_index]
    journey_id = travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id)
    if (
        cursor.leg_id != leg.leg_id
        or cursor.journey_id != journey_id
        or cursor.route_id != leg.route_id
        or cursor.from_place_id != leg.from_place_id
        or cursor.to_place_id != leg.to_place_id
        or cursor.route_fingerprint != leg.route_fingerprint
    ):
        raise InvalidEvent("Travel itinerary cursor does not match its fixed ordered leg.")
    for prior_index in range(cursor.leg_index + 1):
        prior_leg = itinerary.legs[prior_index]
        prior_id = travel_itinerary_journey_id(itinerary.itinerary_id, prior_leg.leg_id)
        prior = journeys_by_id.get(prior_id)
        if prior is None:
            raise InvalidEvent("Travel itinerary cursor has no corresponding committed journey.")
        _validate_journey_for_leg(prior, itinerary, prior_leg)
        if prior.status != "arrived" or prior.resolved_tick is None:
            raise InvalidEvent("Travel itinerary cursor cannot pass an unresolved or interrupted journey.")
    committed = journeys_by_id[journey_id]
    if (
        committed.departed_tick != cursor.departed_tick
        or committed.arrival_tick != cursor.arrived_tick
        or committed.resolved_tick != cursor.arrived_tick
        or committed.hazard_tick is not None
    ):
        raise InvalidEvent("Travel itinerary cursor ticks do not match its committed arrival.")


def _validate_queued_resolution(state: WorldState, journey: TravelJourney) -> None:
    expected_due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
    schedules = [
        item for item in state.scheduled_events
        if item.id == travel_resolution_schedule_id(journey.journey_id)
    ]
    if len(schedules) != 1:
        raise InvalidEvent("An in-transit itinerary leg must have one registered resolution schedule.")
    try:
        schedule = ScheduledEventRecord.model_validate(schedules[0].model_dump(mode="python"))
    except (TypeError, ValueError) as error:
        raise InvalidEvent("The itinerary leg resolution schedule is invalid.") from error
    if (
        schedule.status != "queued"
        or schedule.handler_id != TRAVEL_RESOLUTION_HANDLER_ID
        or schedule.handler_version != TRAVEL_RESOLUTION_HANDLER_VERSION
        or schedule.phase != "resolution"
        or schedule.due_tick != expected_due
        or schedule.max_runs != 1
        or schedule.repeat_ticks is not None
        or schedule.completed_runs != 0
        or schedule.payload != {"journey_id": str(journey.journey_id)}
    ):
        raise InvalidEvent("The in-transit itinerary leg does not match its queued resolution schedule.")
