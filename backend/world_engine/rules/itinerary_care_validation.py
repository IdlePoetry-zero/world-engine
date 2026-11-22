"""Replay validation for the small set of care actions allowed during travel."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import ResourceConsumedEvent
from world_engine.domain.models import DomainEvent, ResourceAccount, TimeAdvancedEvent, WorldState
from world_engine.domain.needs import resource_matches_need
from world_engine.domain.reducers import state_hash, state_json, state_payload_hash
from world_engine.domain.travel_journey_events import (
    TravelJourneyChangedEvent,
    validate_travel_journey_scope,
)
from .contracts import ConsumeIntent, RestIntent, RuleContext, RuleResolution
from .engine import RuleEngine


_ITINERARY_CARE_RECEIPT_CAPABILITY = object()


@dataclass(frozen=True, slots=True, init=False)
class ItineraryCareReceipt:
    """Transient proof of one native rule evaluation for one exact input batch."""

    prestate_hash: str
    ordered_prestate_digest: str
    context_serialization: str
    intent_serialization: str
    elapsed_ticks: int
    event_serializations: tuple[str, ...]
    projected_state: WorldState | None = field(repr=False, compare=False)
    projected_state_hash: str | None
    projected_state_ordered_digest: str | None
    event_ticks: tuple[int, ...]
    _capability: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        capability: object,
        prestate_hash: str,
        ordered_prestate_digest: str,
        context_serialization: str,
        intent_serialization: str,
        elapsed_ticks: int,
        event_serializations: tuple[str, ...],
        projected_state: WorldState | None = None,
        projected_state_hash: str | None = None,
        projected_state_ordered_digest: str | None = None,
        event_ticks: tuple[int, ...] = (),
    ) -> None:
        if capability is not _ITINERARY_CARE_RECEIPT_CAPABILITY:
            raise TypeError("Itinerary care receipts can only be issued by the native evaluator.")
        object.__setattr__(self, "prestate_hash", prestate_hash)
        object.__setattr__(self, "ordered_prestate_digest", ordered_prestate_digest)
        object.__setattr__(self, "context_serialization", context_serialization)
        object.__setattr__(self, "intent_serialization", intent_serialization)
        object.__setattr__(self, "elapsed_ticks", elapsed_ticks)
        object.__setattr__(self, "event_serializations", tuple(event_serializations))
        object.__setattr__(self, "projected_state", projected_state)
        object.__setattr__(self, "projected_state_hash", projected_state_hash)
        object.__setattr__(self, "projected_state_ordered_digest", projected_state_ordered_digest)
        object.__setattr__(self, "event_ticks", tuple(event_ticks))
        object.__setattr__(self, "_capability", capability)

    def matches_binding(
        self,
        *,
        authoritative_prestate_hash: str,
        context_serialization: str,
        intent_serialization: str,
        elapsed_ticks: int,
        event_serializations: tuple[str, ...],
    ) -> bool:
        return (
            self._capability is _ITINERARY_CARE_RECEIPT_CAPABILITY
            and self.prestate_hash == authoritative_prestate_hash
            and self.context_serialization == context_serialization
            and self.intent_serialization == intent_serialization
            and self.elapsed_ticks == elapsed_ticks
            and self.event_serializations == event_serializations
        )

    def matches_ordered_prestate(self, state: WorldState) -> bool:
        return (
            self._capability is _ITINERARY_CARE_RECEIPT_CAPABILITY
            and self.ordered_prestate_digest == _ordered_prestate_digest(state)
        )

    def matches_projected_payload(self, payload: dict[str, object]) -> bool:
        """Bind reuse to state ordering as well as the canonical state hash."""

        return (
            self._capability is _ITINERARY_CARE_RECEIPT_CAPABILITY
            and self.projected_state_ordered_digest is not None
            and self.projected_state_ordered_digest == _ordered_prestate_digest(payload)
        )


def evaluate_itinerary_care(
    state: WorldState,
    intent: ConsumeIntent | RestIntent,
    context: RuleContext,
) -> tuple[RuleResolution, ItineraryCareReceipt | None]:
    """Evaluate one ordinary itinerary-care action and seal only native success."""

    if not isinstance(state, WorldState):
        raise TypeError("Itinerary-care evaluation requires a typed world state.")
    if not isinstance(intent, (ConsumeIntent, RestIntent)) or not isinstance(context, RuleContext):
        raise TypeError("Itinerary-care evaluation requires a typed care intent and rule context.")
    engine = RuleEngine()
    native = type(engine) is RuleEngine
    if native:
        engine._capture_itinerary_care_projection = True
    prestate_hash = state_hash(state) if native else None
    ordered_prestate_digest = _ordered_prestate_digest(state) if native else None
    resolution = engine.evaluate(state, intent, context)
    if (
        not native
        or resolution.outcome.status != "succeeded"
        or resolution.elapsed_ticks <= 0
        or not resolution.events
        or resolution.ruleset_version != context.ruleset_version
    ):
        return resolution, None
    projected_state = resolution._validated_projection
    event_ticks = _care_event_ticks(state, resolution.events, resolution.elapsed_ticks)
    if (
        not isinstance(projected_state, WorldState)
        or projected_state.current_tick != state.current_tick + resolution.elapsed_ticks
        or event_ticks is None
    ):
        return resolution, None
    projected_payload = state_json(projected_state)
    receipt = ItineraryCareReceipt(
        capability=_ITINERARY_CARE_RECEIPT_CAPABILITY,
        prestate_hash=prestate_hash,
        ordered_prestate_digest=ordered_prestate_digest,
        context_serialization=_canonical_model_serialization(context),
        intent_serialization=_canonical_model_serialization(intent),
        elapsed_ticks=resolution.elapsed_ticks,
        event_serializations=tuple(_canonical_model_serialization(event) for event in resolution.events),
        projected_state=projected_state,
        projected_state_hash=state_payload_hash(projected_payload),
        projected_state_ordered_digest=_ordered_prestate_digest(projected_payload),
        event_ticks=event_ticks,
    )
    return resolution, receipt


def _care_event_ticks(
    state: WorldState,
    events: Sequence[DomainEvent],
    elapsed_ticks: int,
) -> tuple[int, ...] | None:
    """Derive ticks only for the exact delay/clock prefix used by travel care."""

    if len(events) < 2:
        return None
    delay, clock = events[0], events[1]
    if (
        not isinstance(delay, TravelJourneyChangedEvent)
        or delay.operation != "delay"
        or delay.stationary_from_tick != state.current_tick
        or not isinstance(clock, TimeAdvancedEvent)
        or clock.to_tick != delay.stationary_until_tick
        or clock.to_tick != state.current_tick + elapsed_ticks
        or any(isinstance(event, TimeAdvancedEvent) for event in events[2:])
    ):
        return None
    return (state.current_tick, *(clock.to_tick for _ in events[1:]))


def _canonical_model_serialization(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _ordered_prestate_digest(value: WorldState | dict[str, object]) -> str:
    snapshot = value.model_dump(mode="json") if isinstance(value, WorldState) else value
    encoded = json.dumps(
        snapshot,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_itinerary_care_batch(
    state: WorldState,
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    receipt: object | None = None,
    authoritative_prestate_hash: str | None = None,
    return_projection: bool = False,
) -> ItineraryCareReceipt | None:
    """Accept only an exact ordinary-rule batch for travel-time rest or care.

    The event batch itself supplies the candidate action: its first event must
    be the actor's typed travel delay. A sole, one-unit actor-owned food or
    water consumption selects ``ConsumeIntent``; with no consumption the
    delay interval selects ``RestIntent``. The ordinary rule engine then
    regenerates the complete batch with an uncustomized context, and its JSON
    events must exactly match the supplied events.
    """

    if not isinstance(state, WorldState):
        raise InvalidEvent("Travel care validation requires a trusted typed world state.")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)) or not events:
        raise InvalidEvent("Travel care validation requires a nonempty typed event batch.")

    typed_events: list[DomainEvent] = []
    for event in events:
        if not isinstance(event, BaseModel):
            raise InvalidEvent("Travel care batches require typed core events.")
        typed_events.append(parse_event(event))

    delay = typed_events[0]
    if (
        not isinstance(delay, TravelJourneyChangedEvent)
        or delay.operation != "delay"
        or delay.actor_id != actor_id
        or delay.stationary_from_tick != state.current_tick
        or delay.stationary_until_tick is None
    ):
        raise InvalidEvent("Travel care batches must begin with the acting traveler's exact delay event.")

    validate_travel_journey_scope(
        typed_events,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
    )

    interval = delay.stationary_until_tick - delay.stationary_from_tick
    if interval <= 0:
        raise InvalidEvent("Travel care delay must describe a positive action interval.")

    consumed = [event for event in typed_events if isinstance(event, ResourceConsumedEvent)]
    if consumed:
        if len(consumed) != 1:
            raise InvalidEvent("Travel-time care may contain only one actor-owned resource flow.")
        intent = _consume_intent_from_flow(state, consumed[0], actor_id, interval)
    else:
        try:
            intent = RestIntent(duration_ticks=interval)
        except (TypeError, ValueError) as error:
            raise InvalidEvent("Travel-time rest does not match a registered rest duration.") from error

    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
        simulation_command_id=command_id,
    )
    if (
        type(receipt) is ItineraryCareReceipt
        and authoritative_prestate_hash is not None
        and receipt.prestate_hash == authoritative_prestate_hash
    ):
        context_serialization = _canonical_model_serialization(context)
        intent_serialization = _canonical_model_serialization(intent)
        event_serializations = tuple(_canonical_model_serialization(event) for event in typed_events)
        expected_event_ticks = _care_event_ticks(state, typed_events, interval)
        if (
            receipt.matches_binding(
                authoritative_prestate_hash=authoritative_prestate_hash,
                context_serialization=context_serialization,
                intent_serialization=intent_serialization,
                elapsed_ticks=interval,
                event_serializations=event_serializations,
            )
            and receipt.matches_ordered_prestate(state)
            and expected_event_ticks is not None
            and receipt.event_ticks == expected_event_ticks
        ):
            if (
                return_projection
                and isinstance(receipt.projected_state, WorldState)
                and receipt.projected_state_hash is not None
                and receipt.projected_state_ordered_digest is not None
                and len(receipt.event_ticks) == len(typed_events)
            ):
                return receipt
            return None

    supplied_json = [event.model_dump(mode="json") for event in typed_events]
    try:
        resolution = RuleEngine().evaluate(state, intent, context)
    except (TypeError, ValueError, ValidationError) as error:
        raise InvalidEvent("Travel-time care could not be replayed through the registered rules.") from error

    expected_json = [event.model_dump(mode="json") for event in resolution.events]
    if resolution.outcome.status != "succeeded" or resolution.elapsed_ticks != interval or supplied_json != expected_json:
        raise InvalidEvent("Travel care must be fully successful and exactly match ordinary rule evaluation.")
    return None


def _consume_intent_from_flow(
    state: WorldState,
    event: ResourceConsumedEvent,
    actor_id: UUID,
    interval: int,
) -> ConsumeIntent:
    if event.consumer_id != actor_id or event.purpose not in {"need:food", "need:water"} or len(event.flows) != 1:
        raise InvalidEvent("Travel-time consumption must be one actor-owned food or water flow.")

    flow = event.flows[0]
    if not Decimal("0.1") <= flow.quantity <= Decimal("1"):
        raise InvalidEvent("Travel-time consumption must use between 0.1 and one resource unit.")
    holder = state.resources.get(flow.holder_account_id)
    sink = state.resources.get(flow.sink_account_id)
    if not isinstance(holder, ResourceAccount) or (
        holder.account_type != "holder"
        or holder.owner_id != actor_id
        or holder.resource_id != flow.resource_id
    ):
        raise InvalidEvent("Travel-time consumption must come from the acting traveler's resource account.")
    if not isinstance(sink, ResourceAccount) or (
        sink.account_type != "consumption_sink"
        or sink.owner_id is not None
        or sink.resource_id != flow.resource_id
    ):
        raise InvalidEvent("Travel-time consumption must use the registered matching consumption sink.")

    need = event.purpose.removeprefix("need:")
    if not resource_matches_need(state.metadata, flow.resource_id, need):
        raise InvalidEvent("Travel-time consumption resource does not match its food or water need.")
    try:
        intent = ConsumeIntent(
            need=need,
            resource_id=flow.resource_id,
            quantity=flow.quantity,
        )
    except (TypeError, ValueError) as error:
        raise InvalidEvent("Travel-time consumption does not match a registered care intent.") from error
    if intent.duration_ticks != interval:
        raise InvalidEvent("Travel-time consumption interval does not match its registered duration.")
    return intent
