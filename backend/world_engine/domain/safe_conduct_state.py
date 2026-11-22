"""Command binding and replay for bilateral safe-conduct treaty events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from .components import CapabilitiesComponent, PlaceComponent
from .errors import InvalidEvent
from .gameplay_events import CombatRetreatResolvedEvent, MovementCompletedEvent, ShipmentDispatchedEvent
from .safe_conduct import (
    SAFE_CONDUCT_TAG, SafeConductTreaty, propose_safe_conduct, registered_safe_conduct_route,
    safe_conduct_allows_route, safe_conduct_counter_options, transition_safe_conduct,
)
from .safe_conduct_events import SafeConductChangedEvent

if TYPE_CHECKING:
    from .models import WorldState


PROPOSE_SAFE_CONDUCT_COMMAND = "action.propose_safe_conduct"
RESOLVE_SAFE_CONDUCT_COMMAND = "action.resolve_safe_conduct"
NPC_SAFE_CONDUCT_COMMAND = "action.request_safe_conduct_response"


def validate_safe_conduct_travel_batch(state: WorldState, events: Sequence[object]) -> None:
    """Recheck treaty-gated departure using the branch state before the command."""
    for event in events:
        if isinstance(event, (MovementCompletedEvent, CombatRetreatResolvedEvent)):
            origin_id, actor_id, departure_tick = event.from_place_id, event.actor_id, event.started_tick
        elif isinstance(event, ShipmentDispatchedEvent):
            origin_id, actor_id, departure_tick = event.origin_place_id, event.sender_id, event.departure_tick
        else:
            continue
        origin = state.entities.get(origin_id)
        if origin is None:
            continue
        place = PlaceComponent.model_validate(origin.components.get("place", {}))
        route = next((route for route in place.routes if route.route_id == event.route_id), None)
        if route is None or SAFE_CONDUCT_TAG not in route.access_tags:
            continue
        actor = state.entities.get(actor_id)
        if actor is None or actor.location_id != origin_id or departure_tick != state.current_tick:
            raise InvalidEvent("Treaty-gated departure requires the actor at its registered origin and tick.")
        capabilities = CapabilitiesComponent.model_validate(actor.components.get("capabilities", {}))
        if SAFE_CONDUCT_TAG in capabilities.restrictions or (
            SAFE_CONDUCT_TAG not in capabilities.grants and
            not safe_conduct_allows_route(state, actor, route, state.safe_conduct_treaties)
        ):
            raise InvalidEvent("Treaty-gated departure has no valid safe conduct at departure.")


def validate_safe_conduct_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID, actor_id: UUID,
    ruleset_version: str, command_type: str | None = None, command_payload: Mapping[str, object] | None = None,
) -> None:
    event = next((item for item in events if isinstance(item, SafeConductChangedEvent)), None)
    if event is None:
        if command_type in {PROPOSE_SAFE_CONDUCT_COMMAND, RESOLVE_SAFE_CONDUCT_COMMAND, NPC_SAFE_CONDUCT_COMMAND}:
            raise InvalidEvent("A safe conduct command requires a committed treaty transition.")
        return
    command_actor_id = event.requested_by or event.actor_id
    if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, command_actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("A safe conduct transition requires one exact command context.")
    if command_type is None:
        return
    treaty = event.resulting_treaty
    if event.requested_by is not None:
        expected_type = NPC_SAFE_CONDUCT_COMMAND
        expected_payload = {"kind": "request_safe_conduct_response", "treaty_id": str(treaty.treaty_id),
                            "expected_revision": event.expected_revision}
    elif event.operation == "propose":
        expected_type = PROPOSE_SAFE_CONDUCT_COMMAND
        expected_payload = {
            "kind": "propose_safe_conduct", "proposer_organization_id": str(treaty.proposer_organization_id),
            "recipient_organization_id": str(treaty.recipient_organization_id),
            "from_place_id": str(treaty.from_place_id), "route_id": str(treaty.route_id),
            "expires_at_tick": treaty.expires_at_tick,
        }
    else:
        expected_type = RESOLVE_SAFE_CONDUCT_COMMAND
        expected_payload = {"kind": "resolve_safe_conduct", "treaty_id": str(treaty.treaty_id),
                            "operation": event.operation, "expected_revision": event.expected_revision}
        if event.operation == "counter":
            expected_payload["counter_expires_at_tick"] = treaty.counter_expires_at_tick
    if command_type != expected_type or command_payload != expected_payload:
        raise InvalidEvent("The treaty transition does not match its command payload.")


def apply_safe_conduct(state: WorldState, event: SafeConductChangedEvent) -> list[SafeConductTreaty]:
    treaty = event.resulting_treaty
    records = state.safe_conduct_treaties
    try:
        if event.operation == "propose":
            if treaty.treaty_id != uuid5(event.command_id, "safe-conduct-treaty"):
                raise InvalidEvent("A treaty must derive from its proposal command.")
            route = registered_safe_conduct_route(state, treaty.from_place_id, treaty.route_id)
            if route is None:
                raise InvalidEvent("The proposed treaty route is unavailable.")
            expected = propose_safe_conduct(
                state, treaty_id=treaty.treaty_id, proposer_organization_id=treaty.proposer_organization_id,
                recipient_organization_id=treaty.recipient_organization_id, proposer_actor_id=event.actor_id,
                route=route, expires_at_tick=treaty.expires_at_tick, existing_treaties=records,
            )
            updated = [*records, expected]
        else:
            prior = next((item for item in records if item.treaty_id == treaty.treaty_id), None)
            if prior is None:
                raise InvalidEvent("The treaty is missing from this branch.")
            if event.requested_by is not None and event.operation == "decline":
                transition_safe_conduct(
                    state, prior, actor_id=event.actor_id, operation="ratify", expected_revision=event.expected_revision,
                )
            if event.requested_by is not None and event.operation == "counter" and (
                treaty.counter_expires_at_tick not in safe_conduct_counter_options(state, prior).values()
            ):
                raise InvalidEvent("A hosted counteroffer must use an available shorter expiry.")
            expected = transition_safe_conduct(
                state, prior, actor_id=event.actor_id, operation=event.operation, expected_revision=event.expected_revision,
                **({"counter_expires_at_tick": treaty.counter_expires_at_tick} if event.operation == "counter" else {}),
            )
            updated = [expected if item.treaty_id == prior.treaty_id else item for item in records]
    except ValueError as exc:
        raise InvalidEvent("Safe conduct is incompatible with its branch state.") from exc
    if treaty != expected:
        raise InvalidEvent("Stored safe conduct does not match the authoritative treaty transition.")
    return updated
