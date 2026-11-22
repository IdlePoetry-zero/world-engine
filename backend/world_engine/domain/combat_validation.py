"""Commit-bound validation for replayable combat event batches.

Call this once for each atomic command batch, and for each archived commit
boundary during restore. Individual event reducers still validate their own
state transitions when the batch context is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from .combat import active_combat_opponent
from .combat_truce import CombatTruceChangedEvent
from .errors import InvalidEvent
from .gameplay_events import (
    CombatExchangeResolvedEvent,
    CombatRetreatResolvedEvent,
    DamageAppliedEvent,
    MovementCompletedEvent,
)
from .models import DomainEvent, WorldState


def validate_combat_batch(
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID | None = None,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
    state: WorldState | None = None,
    causation_ids: Sequence[UUID | str] | None = None,
) -> None:
    """Bind combat audit, damage and retreat movement to one command boundary.

    `command_type`, `command_payload`, and the pre-command `state` are optional
    for archive formats that retain event envelope context but not original
    proposal payloads. Supply them at repository commit when available.
    """
    truce_events = [event for event in events if isinstance(event, CombatTruceChangedEvent)]
    if truce_events:
        event = truce_events[0]
        if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, event.ruleset_version) != (
            world_id, branch_id, command_id, ruleset_version,
        ) or actor_id is not None and actor_id != (event.requested_by or event.actor_id):
            raise InvalidEvent("Combat truce event provenance does not match its command envelope.")
        offer = event.resulting_offer
        cause = offer.last_event_id if event.operation == "offer" else offer.offer_id
        if causation_ids is not None and tuple(map(str, causation_ids)) != (str(cause),):
            raise InvalidEvent("Combat truce must cite the exact prior exchange or offer.")
        if event.operation == "offer":
            kind = "offer_combat_truce"
            expected_payload = {"kind": kind, "expected_last_event_id": str(offer.last_event_id)}
        elif event.requested_by is not None:
            kind = "request_combat_truce_response"
            expected_payload = {"kind": kind, "offer_id": str(offer.offer_id)}
        else:
            kind = "resolve_combat_truce"
            expected_payload = {"kind": kind, "offer_id": str(offer.offer_id), "operation": event.operation}
        if (command_type is not None and command_type != f"action.{kind}"
                or command_payload is not None and command_payload != expected_payload):
            raise InvalidEvent("Combat truce does not match its registered command proposal.")
        return
    if command_type in {
        "action.offer_combat_truce", "action.resolve_combat_truce", "action.request_combat_truce_response",
    }:
        raise InvalidEvent("A combat truce command requires one typed event.")

    exchanges = [event for event in events if isinstance(event, CombatExchangeResolvedEvent)]
    retreats = [event for event in events if isinstance(event, CombatRetreatResolvedEvent)]
    if len(exchanges) > 1 or len(retreats) > 1 or exchanges and retreats:
        raise InvalidEvent("One command batch may contain one attack exchange or one retreat, not both.")
    combat_events = [*exchanges, *retreats]
    for event in combat_events:
        if (
            event.world_id != world_id
            or event.branch_id != branch_id
            or event.command_id != command_id
            or event.ruleset_version != ruleset_version
            or actor_id is not None and event.actor_id != actor_id
        ):
            raise InvalidEvent("Combat event provenance does not match its command envelope.")

    if exchanges:
        exchange = exchanges[0]
        if command_type is not None and command_type != "action.attack":
            raise InvalidEvent("A combat exchange must be committed by an attack action.")
        if command_payload is not None:
            if (
                command_payload.get("kind") != "attack"
                or str(command_payload.get("target_id")) != str(exchange.target_id)
                or command_payload.get("weapon_resource_id") != exchange.weapon_resource_id
            ):
                raise InvalidEvent("Combat exchange does not match the command's registered attack proposal.")
        if state is not None:
            actor = state.entities.get(exchange.actor_id)
            if actor is None or exchange.started_tick != state.current_tick:
                raise InvalidEvent("Combat exchange start tick or actor does not match the command's base state.")
            current = active_combat_opponent(state, actor)
            if current is not None and current[1].id != exchange.target_id:
                raise InvalidEvent("Combat command attempts a second encounter for an active character.")
        if exchange.outcome == "hit":
            damage_events = [event for event in events if isinstance(event, DamageAppliedEvent)
                             and event.event_id == exchange.damage_event_id]
            if len(damage_events) != 1:
                raise InvalidEvent("A recorded combat hit must include its matching damage event in the same batch.")
            damage = damage_events[0]
            if (
                damage.target_id != exchange.target_id
                or damage.source_id != exchange.actor_id
                or damage.damage != exchange.damage
                or damage.expected_health_revision != exchange.expected_target_health_revision
                or damage.source_event_ids != [exchange.event_id]
                or damage.injury_type != ("combat.weapon" if exchange.weapon_resource_id else "core.injury.blunt")
                or events.index(exchange) >= events.index(damage)
            ):
                raise InvalidEvent("Combat damage does not exactly match its preceding exchange audit event.")
        elif any(
            isinstance(event, DamageAppliedEvent) and exchange.event_id in event.source_event_ids
            for event in events
        ):
            raise InvalidEvent("A missed combat exchange cannot have a linked damage event.")

    if retreats:
        retreat = retreats[0]
        if command_type is not None and command_type != "action.retreat":
            raise InvalidEvent("A combat retreat must be committed by a retreat action.")
        if command_payload is not None and (
            command_payload.get("kind") != "retreat"
            or str(command_payload.get("destination_id")) != str(retreat.to_place_id)
            or command_payload.get("route_id") is not None
            and str(command_payload.get("route_id")) != str(retreat.route_id)
        ):
            raise InvalidEvent("Combat retreat does not match the command's registered route proposal.")
        movements = [event for event in events if isinstance(event, MovementCompletedEvent)
                     and event.actor_id == retreat.actor_id]
        if len(movements) != 1:
            raise InvalidEvent("A successful retreat must include exactly one matching route movement.")
        movement = movements[0]
        if (
            movement.route_id != retreat.route_id
            or movement.from_place_id != retreat.from_place_id
            or movement.to_place_id != retreat.to_place_id
            or movement.started_tick != retreat.started_tick
            or movement.arrived_tick != retreat.arrived_tick
            or events.index(retreat) >= events.index(movement)
        ):
            raise InvalidEvent("Retreat outcome must precede its exact registered movement event.")
        if state is not None:
            actor = state.entities.get(retreat.actor_id)
            if actor is None or retreat.started_tick != state.current_tick:
                raise InvalidEvent("Retreat start tick or actor does not match the command's base state.")
            current = active_combat_opponent(state, actor)
            if current is None or current[0].encounter_id != retreat.encounter_id or current[1].id != retreat.opponent_id:
                raise InvalidEvent("Retreat does not resolve the actor's active co-located encounter.")

    if state is not None:
        retreat_actor_ids = {event.actor_id for event in retreats}
        for movement in (event for event in events if isinstance(event, MovementCompletedEvent)):
            if movement.actor_id in retreat_actor_ids:
                continue
            actor = state.entities.get(movement.actor_id)
            if actor is not None and active_combat_opponent(state, actor) is not None:
                raise InvalidEvent("An active combatant must use the explicit retreat action to move.")
