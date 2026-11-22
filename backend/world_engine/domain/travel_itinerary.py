"""Immutable plans for explicitly ordered, registered-route itineraries.

An itinerary records the routes a player selected in order. It is not a path
finder and does not grant current travel permission. At each leg departure the
worker must revalidate the route snapshot and then let the ordinary movement
rules make the authoritative access decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import PlaceComponent, PlaceRoute
from .errors import InvalidEvent
from .travel_journeys import TravelJourney

if TYPE_CHECKING:
    from .models import WorldState


TRAVEL_ITINERARY_VERSION = "travel_itinerary.v1"
MAX_TRAVEL_ITINERARY_LEGS = 256
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)

TravelItineraryTick = Annotated[StrictInt, Field(ge=_MIN_TICK, le=_MAX_TICK)]
TravelItineraryDuration = Annotated[StrictInt, Field(gt=0, le=_MAX_TICK)]
TravelItineraryDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class TravelItineraryLeg(_FrozenModel):
    """One exact directed route snapshot in the player's ordered itinerary."""

    leg_id: UUID
    leg_index: int = Field(ge=0, lt=MAX_TRAVEL_ITINERARY_LEGS)
    route_id: UUID
    from_place_id: UUID
    to_place_id: UUID
    travel_ticks: TravelItineraryDuration
    route_fingerprint: TravelItineraryDigest

    @model_validator(mode="after")
    def distinct_endpoints(self) -> TravelItineraryLeg:
        if self.from_place_id == self.to_place_id:
            raise ValueError("an itinerary leg requires distinct endpoints")
        return self


class TravelItinerary(_FrozenModel):
    """Fixed preauthorization of a bounded, explicitly ordered route list.

    `deadline_tick` is an absolute world tick by which all route durations
    must fit. Runtime cursor/progress is deliberately stored by the durable job,
    not mutated into this plan.
    """

    contract_version: Literal["travel_itinerary.v1"] = TRAVEL_ITINERARY_VERSION
    itinerary_id: UUID
    actor_id: UUID
    origin_place_id: UUID
    destination_place_id: UUID
    created_tick: TravelItineraryTick
    deadline_tick: TravelItineraryTick
    legs: tuple[TravelItineraryLeg, ...] = Field(
        min_length=1,
        max_length=MAX_TRAVEL_ITINERARY_LEGS,
    )

    @model_validator(mode="after")
    def connected_legs_fit_contract(self) -> TravelItinerary:
        if self.deadline_tick < self.created_tick:
            raise ValueError("itinerary deadline must not precede its creation tick")
        if self.legs[0].from_place_id != self.origin_place_id:
            raise ValueError("the first itinerary leg must start at the recorded origin")
        if self.legs[-1].to_place_id != self.destination_place_id:
            raise ValueError("the last itinerary leg must end at the recorded destination")
        for index, leg in enumerate(self.legs):
            if leg.leg_index != index:
                raise ValueError("itinerary legs must use their fixed ordered indices")
            if leg.leg_id != travel_itinerary_leg_id(self.itinerary_id, index, leg.route_id):
                raise ValueError("itinerary leg ID is not bound to this itinerary and ordered route")
            if index and self.legs[index - 1].to_place_id != leg.from_place_id:
                raise ValueError("itinerary legs must form an explicitly connected route sequence")
        if self.created_tick + self.total_travel_ticks > self.deadline_tick:
            raise ValueError("the registered route durations cannot fit before the itinerary deadline")
        if self.created_tick + self.total_travel_ticks > _MAX_TICK:
            raise ValueError("itinerary travel exceeds the supported world clock")
        return self

    @property
    def total_travel_ticks(self) -> int:
        return sum(leg.travel_ticks for leg in self.legs)


class TravelItineraryArrivalProof(_FrozenModel):
    """Caller checkpoint for the immediately preceding committed leg arrival.

    The fixed journey ID is derived from the itinerary and leg IDs. The durable
    worker must bind this receipt to that leg's earlier committed departure;
    revalidation also checks the matching arrived journey in trusted WorldState.
    Location alone is never accepted as proof that an earlier leg completed.
    """

    contract_version: Literal["travel_itinerary.v1"] = TRAVEL_ITINERARY_VERSION
    itinerary_id: UUID
    leg_id: UUID
    leg_index: int = Field(ge=0, lt=MAX_TRAVEL_ITINERARY_LEGS)
    journey_id: UUID
    actor_id: UUID
    route_id: UUID
    from_place_id: UUID
    to_place_id: UUID
    route_fingerprint: TravelItineraryDigest
    departed_tick: TravelItineraryTick
    arrived_tick: TravelItineraryTick

    @model_validator(mode="after")
    def valid_identity_and_timing(self) -> TravelItineraryArrivalProof:
        if self.journey_id != travel_itinerary_journey_id(self.itinerary_id, self.leg_id):
            raise ValueError("arrival proof journey ID must be bound to its itinerary leg")
        if self.from_place_id == self.to_place_id:
            raise ValueError("arrival proof requires distinct route endpoints")
        if self.arrived_tick <= self.departed_tick:
            raise ValueError("arrival proof must follow its recorded departure")
        return self


TravelItineraryRevalidationOutcome = Literal[
    "current",
    "route_invalidated",
    "route_unobserved",
    "actor_unavailable",
    "actor_in_transit",
    "actor_not_at_origin",
    "arrival_proof_required",
    "arrival_proof_invalid",
    "deadline_elapsed",
    "deadline_unreachable",
]

TravelItineraryRevalidationReason = Literal[
    "actor_missing_or_terminal",
    "actor_already_traveling",
    "actor_location_changed",
    "origin_unavailable",
    "destination_unavailable",
    "route_map_invalid",
    "route_not_registered",
    "route_closed",
    "route_snapshot_changed",
    "destination_not_observed",
    "arrival_proof_missing",
    "arrival_proof_mismatch",
    "arrival_record_missing",
    "deadline_passed",
    "remaining_route_cannot_fit_deadline",
]


class TravelItineraryLegRevalidation(_FrozenModel):
    """Typed, non-authorizing result of checking one planned leg at departure."""

    itinerary_id: UUID
    leg_id: UUID
    leg_index: int = Field(ge=0, lt=MAX_TRAVEL_ITINERARY_LEGS)
    outcome: TravelItineraryRevalidationOutcome
    reason: TravelItineraryRevalidationReason | None = None
    route_snapshot: Literal["matched", "mismatched", "unavailable", "unchecked"]
    destination_observation: Literal["not_required", "observed", "unobserved", "unchecked"]
    travel_permission: Literal["not_evaluated"] = "not_evaluated"

    @model_validator(mode="after")
    def outcome_has_consistent_reason(self) -> TravelItineraryLegRevalidation:
        reasons = {
            "current": {None},
            "route_invalidated": {
                "origin_unavailable", "destination_unavailable", "route_map_invalid",
                "route_not_registered", "route_closed", "route_snapshot_changed",
            },
            "route_unobserved": {"destination_not_observed"},
            "actor_unavailable": {"actor_missing_or_terminal"},
            "actor_in_transit": {"actor_already_traveling"},
            "actor_not_at_origin": {"actor_location_changed"},
            "arrival_proof_required": {"arrival_proof_missing"},
            "arrival_proof_invalid": {"arrival_proof_mismatch", "arrival_record_missing"},
            "deadline_elapsed": {"deadline_passed"},
            "deadline_unreachable": {"remaining_route_cannot_fit_deadline"},
        }
        if self.reason not in reasons[self.outcome]:
            raise ValueError("itinerary revalidation outcome and reason do not match")
        if self.outcome in {"current", "route_unobserved"} and self.route_snapshot != "matched":
            raise ValueError("a current or unobserved leg must match its route snapshot")
        if self.outcome == "route_unobserved" and self.destination_observation != "unobserved":
            raise ValueError("an unobserved route result must record its observation status")
        if self.outcome == "route_invalidated" and self.route_snapshot not in {"mismatched", "unavailable"}:
            raise ValueError("an invalidated route must have a mismatched or unavailable snapshot")
        if self.outcome not in {"current", "route_unobserved", "route_invalidated"} and (
            self.route_snapshot != "unchecked" or self.destination_observation != "unchecked"
        ):
            raise ValueError("non-route revalidation outcomes cannot claim route checks")
        return self


def travel_itinerary_leg_id(itinerary_id: UUID, leg_index: int, route_id: UUID) -> UUID:
    """Return the stable identity for one route occurrence in an itinerary."""

    if not isinstance(itinerary_id, UUID) or not isinstance(route_id, UUID):
        raise ValueError("itinerary leg IDs require UUID itinerary and route IDs")
    if type(leg_index) is not int or not 0 <= leg_index < MAX_TRAVEL_ITINERARY_LEGS:
        raise ValueError("itinerary leg index is outside the bounded route list")
    return uuid5(itinerary_id, f"{TRAVEL_ITINERARY_VERSION}:leg:{leg_index}:{route_id}")


def travel_itinerary_journey_id(itinerary_id: UUID, leg_id: UUID) -> UUID:
    """Return the stable journey identity a durable worker binds to a leg."""

    if not isinstance(itinerary_id, UUID) or not isinstance(leg_id, UUID):
        raise ValueError("itinerary journey IDs require UUID itinerary and leg IDs")
    return uuid5(itinerary_id, f"{TRAVEL_ITINERARY_VERSION}:journey:{leg_id}")


def travel_route_fingerprint(route: PlaceRoute | dict[str, object]) -> str:
    """Hash the exact canonical serialized registered route definition."""

    try:
        typed_route = PlaceRoute.model_validate(
            route.model_dump(mode="json") if isinstance(route, PlaceRoute) else route,
        )
    except (TypeError, ValueError) as error:
        raise InvalidEvent("The registered route cannot be fingerprinted.") from error

    # Reuse the domain's canonical author-value hashing over the complete
    # canonical model dump. No compatibility-default elision applies because
    # this is a route definition snapshot, not a whole-state hash.
    from .reducers import author_value_hash

    return author_value_hash(typed_route.model_dump(mode="json"))


def plan_travel_itinerary(
    state: WorldState,
    *,
    itinerary_id: UUID,
    actor_id: UUID,
    route_ids: Sequence[UUID],
    deadline_tick: int,
) -> TravelItinerary:
    """Build a connected itinerary from only the explicitly selected route IDs.

    This resolves IDs against the trusted world registry; it does not discover
    a graph path, infer user intent, infer actor knowledge, or authorize travel.
    Current access and hidden-destination observation are separate checks at
    each actual leg departure.
    """

    if not isinstance(itinerary_id, UUID) or not isinstance(actor_id, UUID):
        raise InvalidEvent("Travel itineraries require UUID itinerary and actor IDs.")
    if isinstance(route_ids, (str, bytes)) or not isinstance(route_ids, Sequence):
        raise InvalidEvent("Travel itinerary routes must be an explicitly ordered bounded list.")
    if not 1 <= len(route_ids) <= MAX_TRAVEL_ITINERARY_LEGS:
        raise InvalidEvent("Travel itinerary must contain between 1 and 256 explicit route IDs.")
    if any(not isinstance(route_id, UUID) for route_id in route_ids):
        raise InvalidEvent("Travel itinerary route IDs must be UUIDs from the selected world.")
    if type(deadline_tick) is not int or not _MIN_TICK <= deadline_tick <= _MAX_TICK:
        raise InvalidEvent("Travel itinerary deadline must be a signed world tick.")

    entities = getattr(state, "entities", None)
    current_tick = getattr(state, "current_tick", None)
    if not isinstance(entities, Mapping) or type(current_tick) is not int:
        raise InvalidEvent("Travel itinerary planning requires a trusted world state.")
    if not _MIN_TICK <= current_tick <= _MAX_TICK:
        raise InvalidEvent("Travel itinerary start tick is outside the supported world clock.")
    if deadline_tick < current_tick:
        raise InvalidEvent("Travel itinerary deadline cannot precede its start tick.")

    actor = entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise InvalidEvent("Travel itinerary actor must be an active character.")
    if actor.location_id is None:
        raise InvalidEvent("Travel itinerary actor must start at a registered place.")
    if any(
        journey.actor_id == actor_id and journey.status == "in_transit"
        for journey in getattr(state, "travel_journeys", ())
    ):
        raise InvalidEvent("Travel itinerary actor already has an active journey.")

    origin_place_id = actor.location_id
    cursor_place_id = origin_place_id
    legs: list[TravelItineraryLeg] = []
    for index, route_id in enumerate(route_ids):
        origin = entities.get(cursor_place_id)
        if origin is None or origin.kind not in {"place", "location"} or origin.status != "active":
            raise InvalidEvent("Travel itinerary route origin must be an active registered place.")
        try:
            origin_component = PlaceComponent.model_validate(origin.components.get("place"))
        except (TypeError, ValueError) as error:
            raise InvalidEvent("Travel itinerary route origin has no valid registered route map.") from error
        matches = [route for route in origin_component.routes if route.route_id == route_id]
        if len(matches) != 1:
            raise InvalidEvent("Each itinerary leg must name exactly one route registered at its origin.")
        route = matches[0]
        if route.from_place_id != cursor_place_id:
            raise InvalidEvent("Travel itinerary route direction does not match its connected origin.")
        destination = entities.get(route.to_place_id)
        if destination is None or destination.kind not in {"place", "location"} or destination.status != "active":
            raise InvalidEvent("Travel itinerary route destination must be an active registered place.")
        leg = TravelItineraryLeg(
            leg_id=travel_itinerary_leg_id(itinerary_id, index, route.route_id),
            leg_index=index,
            route_id=route.route_id,
            from_place_id=route.from_place_id,
            to_place_id=route.to_place_id,
            travel_ticks=route.travel_ticks,
            route_fingerprint=travel_route_fingerprint(route),
        )
        legs.append(leg)
        cursor_place_id = route.to_place_id

    try:
        return TravelItinerary(
            itinerary_id=itinerary_id,
            actor_id=actor_id,
            origin_place_id=origin_place_id,
            destination_place_id=cursor_place_id,
            created_tick=current_tick,
            deadline_tick=deadline_tick,
            legs=tuple(legs),
        )
    except (TypeError, ValueError) as error:
        raise InvalidEvent("The selected route sequence cannot fit the itinerary contract.") from error


def revalidate_travel_itinerary_leg(
    state: WorldState,
    itinerary: TravelItinerary,
    leg_index: int,
    *,
    previous_arrival: TravelItineraryArrivalProof | None = None,
) -> TravelItineraryLegRevalidation:
    """Revalidate a selected leg at departure without granting travel access.

    Leg zero requires the actor to remain at the recorded itinerary origin and
    accepts no arrival proof. Every later leg requires a proof for the
    immediately preceding itinerary leg plus its exact arrived journey in the
    trusted state. The worker must persist the proof from the earlier committed
    departure/arrival pair; merely finding the actor at the right place cannot
    advance the itinerary.

    A `current` outcome means the immutable route snapshot still matches and
    any hidden destination is observed by this actor. It intentionally reports
    `travel_permission='not_evaluated'`; route access rules must still authorize
    the real departure. Explicitly listing a route in the itinerary is user
    preauthorization, not a substitute for that live rules decision.
    """

    if type(leg_index) is not int or not 0 <= leg_index < len(itinerary.legs):
        raise InvalidEvent("Travel itinerary leg index is outside its fixed route list.")
    leg = itinerary.legs[leg_index]
    base = {
        "itinerary_id": itinerary.itinerary_id,
        "leg_id": leg.leg_id,
        "leg_index": leg_index,
    }

    def result(
        outcome: TravelItineraryRevalidationOutcome,
        reason: TravelItineraryRevalidationReason | None,
        *,
        route_snapshot: Literal["matched", "mismatched", "unavailable", "unchecked"] = "unchecked",
        destination_observation: Literal["not_required", "observed", "unobserved", "unchecked"] = "unchecked",
    ) -> TravelItineraryLegRevalidation:
        return TravelItineraryLegRevalidation(
            **base,
            outcome=outcome,
            reason=reason,
            route_snapshot=route_snapshot,
            destination_observation=destination_observation,
        )

    entities = getattr(state, "entities", None)
    current_tick = getattr(state, "current_tick", None)
    if not isinstance(entities, Mapping) or type(current_tick) is not int:
        raise InvalidEvent("Travel itinerary revalidation requires a trusted world state.")

    actor = entities.get(itinerary.actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        return result("actor_unavailable", "actor_missing_or_terminal")
    if actor.location_id is None:
        return result("actor_in_transit", "actor_already_traveling")

    if leg_index == 0:
        if previous_arrival is not None:
            return result("arrival_proof_invalid", "arrival_proof_mismatch")
        if actor.location_id != itinerary.origin_place_id:
            return result("actor_not_at_origin", "actor_location_changed")
    else:
        if previous_arrival is None:
            return result("arrival_proof_required", "arrival_proof_missing")
        if not _arrival_proof_matches_plan(previous_arrival, itinerary, leg_index - 1):
            return result("arrival_proof_invalid", "arrival_proof_mismatch")
        stored_journeys = getattr(state, "travel_journeys", ())
        matching = [journey for journey in stored_journeys if journey.journey_id == previous_arrival.journey_id]
        if len(matching) != 1:
            return result("arrival_proof_invalid", "arrival_record_missing")
        journey = matching[0]
        if not _arrival_journey_matches_proof(journey, previous_arrival):
            return result("arrival_proof_invalid", "arrival_proof_mismatch")
        if actor.location_id != previous_arrival.to_place_id:
            return result("actor_not_at_origin", "actor_location_changed")

    if current_tick > itinerary.deadline_tick:
        return result("deadline_elapsed", "deadline_passed")
    remaining_ticks = sum(item.travel_ticks for item in itinerary.legs[leg_index:])
    if current_tick + remaining_ticks > itinerary.deadline_tick:
        return result("deadline_unreachable", "remaining_route_cannot_fit_deadline")

    origin = entities.get(leg.from_place_id)
    destination = entities.get(leg.to_place_id)
    if origin is None or origin.kind not in {"place", "location"} or origin.status != "active":
        return result("route_invalidated", "origin_unavailable", route_snapshot="unavailable")
    if destination is None or destination.kind not in {"place", "location"} or destination.status != "active":
        return result("route_invalidated", "destination_unavailable", route_snapshot="unavailable")
    try:
        origin_component = PlaceComponent.model_validate(origin.components.get("place"))
    except (TypeError, ValueError):
        return result("route_invalidated", "route_map_invalid", route_snapshot="unavailable")
    routes = [route for route in origin_component.routes if route.route_id == leg.route_id]
    if len(routes) != 1:
        return result("route_invalidated", "route_not_registered", route_snapshot="unavailable")
    route = routes[0]
    if not route.is_open:
        return result("route_invalidated", "route_closed", route_snapshot="mismatched")
    if (
        route.from_place_id != leg.from_place_id
        or route.to_place_id != leg.to_place_id
        or route.travel_ticks != leg.travel_ticks
        or travel_route_fingerprint(route) != leg.route_fingerprint
    ):
        return result("route_invalidated", "route_snapshot_changed", route_snapshot="mismatched")

    observation = _destination_observation(state, itinerary.actor_id, destination)
    if observation == "unobserved":
        return result(
            "route_unobserved",
            "destination_not_observed",
            route_snapshot="matched",
            destination_observation="unobserved",
        )
    return result(
        "current",
        None,
        route_snapshot="matched",
        destination_observation=observation,
    )


def _arrival_proof_matches_plan(
    proof: TravelItineraryArrivalProof,
    itinerary: TravelItinerary,
    leg_index: int,
) -> bool:
    leg = itinerary.legs[leg_index]
    return (
        proof.itinerary_id == itinerary.itinerary_id
        and proof.actor_id == itinerary.actor_id
        and proof.leg_id == leg.leg_id
        and proof.leg_index == leg_index
        and proof.journey_id == travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id)
        and proof.route_id == leg.route_id
        and proof.from_place_id == leg.from_place_id
        and proof.to_place_id == leg.to_place_id
        and proof.route_fingerprint == leg.route_fingerprint
    )


def _arrival_journey_matches_proof(
    journey: TravelJourney,
    proof: TravelItineraryArrivalProof,
) -> bool:
    return (
        journey.journey_id == proof.journey_id
        and journey.actor_id == proof.actor_id
        and journey.route_id == proof.route_id
        and journey.from_place_id == proof.from_place_id
        and journey.to_place_id == proof.to_place_id
        and journey.departed_tick == proof.departed_tick
        and journey.arrival_tick == proof.arrived_tick
        and journey.status == "arrived"
        and journey.resolved_tick == proof.arrived_tick
        and journey.hazard_tick is None
    )


def _destination_observation(
    state: WorldState,
    actor_id: UUID,
    destination,
) -> Literal["not_required", "observed", "unobserved"]:
    if destination.components.get("hidden") is not True:
        return "not_required"
    current_tick = state.current_tick
    return (
        "observed"
        if any(
            record.subject_id == destination.id
            and actor_id in {record.holder_id, record.owner_id}
            and record.kind == "observation"
            and record.received_tick <= current_tick
            and (record.valid_from_tick is None or record.valid_from_tick <= current_tick)
            for record in state.knowledge
        )
        else "unobserved"
    )
