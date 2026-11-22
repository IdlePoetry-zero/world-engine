"""Typed, consent-preserving offers to end a live combat encounter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .components import CombatEncounterComponent
from .errors import InvalidEvent
from .json_types import JsonObject

if TYPE_CHECKING:
    from .models import WorldEntity, WorldState


COMBAT_TRUCE_CHANGED_EVENT_TYPE = "core.combat_truce_changed.v1"
CombatTruceOperation = Literal["offer", "accept", "decline"]
CombatTruceStatus = Literal["pending", "accepted", "declined"]
Tick = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
_MAX_REVISION = 2**63 - 1


def combat_truce_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: CombatTruceOperation,
    offer_id: UUID | None = None,
) -> UUID:
    """Derive a deterministic event ID; an offer's first event is its offer ID."""

    if not isinstance(command_id, UUID):
        raise ValueError("combat truce event IDs require a UUID command ID")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("combat truce event IDs require a nonempty ruleset version")
    if operation not in {"offer", "accept", "decline"}:
        raise ValueError("combat truce operation is unsupported")
    if operation == "offer":
        if offer_id is not None:
            raise ValueError("a new combat truce offer ID derives from its offer command")
        return uuid5(command_id, f"{ruleset_version}:combat_truce.offer")
    if not isinstance(offer_id, UUID):
        raise ValueError("a combat truce response event ID requires its UUID offer ID")
    return uuid5(command_id, f"{ruleset_version}:combat_truce.{operation}:{offer_id}")


def combat_truce_visibility(proposer_id: UUID, recipient_id: UUID) -> JsonObject:
    """Return the canonical audience containing exactly the encounter parties."""

    if not isinstance(proposer_id, UUID) or not isinstance(recipient_id, UUID):
        raise ValueError("combat truce visibility requires UUID participant IDs")
    if proposer_id == recipient_id:
        raise ValueError("combat truce visibility requires distinct participants")
    return {"policy": "subject_ids", "subject_ids": sorted((str(proposer_id), str(recipient_id)))}


class CombatTruceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class CombatTruceOffer(CombatTruceModel):
    """An immutable snapshot of one party's offer during one combat exchange version."""

    offer_id: UUID
    encounter_id: UUID
    proposer_id: UUID
    recipient_id: UUID
    location_id: UUID
    exchange_count: StrictInt = Field(ge=1, le=_MAX_REVISION)
    last_event_id: UUID
    offered_tick: Tick
    status: CombatTruceStatus = "pending"
    responded_tick: Tick | None = None

    @model_validator(mode="after")
    def offer_lifecycle_is_consistent(self) -> CombatTruceOffer:
        if self.proposer_id == self.recipient_id:
            raise ValueError("a combat truce offer requires two distinct participants")
        if self.status == "pending":
            if self.responded_tick is not None:
                raise ValueError("a pending combat truce offer cannot have a response tick")
        elif self.responded_tick is None or self.responded_tick < self.offered_tick:
            raise ValueError("a resolved combat truce offer requires a response tick at or after its offer")
        return self


class CombatTruceChangedEvent(CombatTruceModel):
    """Bind one voluntary offer or recipient response to a scoped command."""

    type: Literal["core.combat_truce_changed.v1"] = COMBAT_TRUCE_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    operation: CombatTruceOperation
    resulting_offer: CombatTruceOffer
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    visibility: JsonObject

    @model_validator(mode="after")
    def transition_is_command_bound_and_participant_private(self) -> CombatTruceChangedEvent:
        offer = self.resulting_offer
        expected_event_id = combat_truce_event_id(
            self.command_id,
            self.ruleset_version,
            self.operation,
            None if self.operation == "offer" else offer.offer_id,
        )
        if self.event_id != expected_event_id:
            raise ValueError("combat truce event ID must be bound to its command, operation, and offer")
        if self.visibility != combat_truce_visibility(offer.proposer_id, offer.recipient_id):
            raise ValueError("combat truce details must be visible only to its two participants")

        if self.operation == "offer":
            if (
                offer.offer_id != self.event_id
                or offer.status != "pending"
                or offer.responded_tick is not None
                or self.actor_id != offer.proposer_id
                or self.requested_by is not None
            ):
                raise ValueError("a combat truce offer must be a new pending offer made by its proposer")
        else:
            expected_status = "accepted" if self.operation == "accept" else "declined"
            if (
                offer.status != expected_status
                or offer.responded_tick is None
                or self.actor_id != offer.recipient_id
            ):
                raise ValueError("only the offer recipient may accept or decline a pending combat truce")
            if self.requested_by is not None and self.requested_by != offer.proposer_id:
                raise ValueError("a hosted combat truce response must be requested by its proposer")
        return self


COMBAT_TRUCE_EVENT_ADAPTER = TypeAdapter(CombatTruceChangedEvent)


def parse_combat_truce_event(
    value: CombatTruceChangedEvent | dict[str, object],
) -> CombatTruceChangedEvent:
    """Parse a standalone combat truce event DTO."""

    return COMBAT_TRUCE_EVENT_ADAPTER.validate_python(value)


def plan_combat_truce(
    state: WorldState,
    *,
    operation: CombatTruceOperation,
    actor_id: UUID,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    existing_offers: Sequence[CombatTruceOffer | dict[str, object]] = (),
    offer_id: UUID | None = None,
    requested_by: UUID | None = None,
) -> CombatTruceChangedEvent:
    """Plan one offer or response without mutating the world or combat state.

    ``existing_offers`` is explicit by design: the host owns persistence and
    authorization, while the planner enforces consent, current encounter
    identity, and the one-pending-offer rule.
    """

    _require_state_and_scope(
        state,
        operation=operation,
        actor_id=actor_id,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
    )
    offers = _validated_offers(existing_offers)

    if operation == "offer":
        if offer_id is not None:
            raise InvalidEvent("A new combat truce offer ID is derived from its command.")
        if requested_by is not None:
            raise InvalidEvent("A combat truce offer cannot be requested by another character.")
        pair = _active_pair(state, actor_id)
        if pair is None:
            raise InvalidEvent("A combat truce can only be offered in the actor's active, co-located encounter.")
        encounter, opponent = pair
        if any(
            offer.status == "pending"
            and offer.encounter_id == encounter.encounter_id
            and {offer.proposer_id, offer.recipient_id} == {actor_id, opponent.id}
            and is_current_combat_truce_offer(state, offer)
            for offer in offers
        ):
            raise InvalidEvent("This combat encounter already has a current pending truce offer.")

        new_offer_id = combat_truce_event_id(command_id, ruleset_version, "offer")
        offer = CombatTruceOffer(
            offer_id=new_offer_id,
            encounter_id=encounter.encounter_id,
            proposer_id=actor_id,
            recipient_id=opponent.id,
            location_id=encounter.location_id,
            exchange_count=encounter.exchange_count,
            last_event_id=encounter.last_event_id,
            offered_tick=state.current_tick,
        )
    else:
        if not isinstance(offer_id, UUID):
            raise InvalidEvent("A combat truce response requires an offer ID.")
        matches = [item for item in offers if item.offer_id == offer_id]
        if len(matches) != 1:
            raise InvalidEvent("The combat truce offer does not exist exactly once.")
        current = matches[0]
        if current.status != "pending":
            raise InvalidEvent("Only a pending combat truce offer can be accepted or declined.")
        if actor_id != current.recipient_id:
            raise InvalidEvent("Only the other combat participant may accept or decline this truce offer.")
        if requested_by is not None and requested_by != current.proposer_id:
            raise InvalidEvent("A hosted combat truce response must be requested by the offer's proposer.")
        if not is_current_combat_truce_offer(state, current):
            raise InvalidEvent("The combat truce offer is stale because the encounter has changed or ended.")
        _require_active_actor(state, actor_id)
        if state.current_tick < current.offered_tick:
            raise InvalidEvent("The combat truce offer is from the future relative to this world state.")
        offer = current.model_copy(update={
            "status": "accepted" if operation == "accept" else "declined",
            "responded_tick": state.current_tick,
        })

    return CombatTruceChangedEvent(
        event_id=combat_truce_event_id(
            command_id,
            ruleset_version,
            operation,
            None if operation == "offer" else offer.offer_id,
        ),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
        operation=operation,
        resulting_offer=offer,
        requested_by=requested_by,
        visibility=combat_truce_visibility(offer.proposer_id, offer.recipient_id),
    )


def is_current_combat_truce_offer(state: WorldState, offer: CombatTruceOffer) -> bool:
    """Return whether a pending offer still names the exact active encounter version."""

    _require_state(state)
    record = _validated_offer(offer)
    if record.status != "pending" or record.offered_tick > state.current_tick:
        return False
    proposer = state.entities.get(record.proposer_id)
    recipient = state.entities.get(record.recipient_id)
    if any(
        entity is None or entity.kind not in {"character", "person"} or entity.status != "active"
        for entity in (proposer, recipient)
    ):
        return False
    pair = _active_pair(state, record.proposer_id)
    if pair is None:
        return False
    encounter, opponent = pair
    return (
        opponent.id == record.recipient_id
        and encounter.encounter_id == record.encounter_id
        and encounter.location_id == record.location_id
        and encounter.exchange_count == record.exchange_count
        and encounter.last_event_id == record.last_event_id
    )


def current_combat_truce_offer(
    state: WorldState,
    actor_id: UUID,
    existing_offers: Sequence[CombatTruceOffer | dict[str, object]] = (),
) -> CombatTruceOffer | None:
    """Find the unique pending offer for this actor's exact active encounter, if any."""

    _require_state(state)
    _require_active_actor(state, actor_id)
    offers = _validated_offers(existing_offers)
    pair = _active_pair(state, actor_id)
    if pair is None:
        return None
    encounter, opponent = pair
    matches = [
        offer for offer in offers
        if offer.status == "pending"
        and offer.encounter_id == encounter.encounter_id
        and {offer.proposer_id, offer.recipient_id} == {actor_id, opponent.id}
        and is_current_combat_truce_offer(state, offer)
    ]
    if len(matches) > 1:
        raise InvalidEvent("More than one current pending truce offer exists for this combat encounter.")
    return matches[0] if matches else None


def _active_pair(
    state: WorldState,
    actor_id: UUID,
) -> tuple[CombatEncounterComponent, WorldEntity] | None:
    from .combat import active_combat_opponent

    _require_active_actor(state, actor_id)
    actor = state.entities.get(actor_id)
    if actor is None:
        return None
    return active_combat_opponent(state, actor)


def _require_active_actor(state: WorldState, actor_id: UUID) -> None:
    if not isinstance(actor_id, UUID):
        raise InvalidEvent("Combat truce actor ID must be a UUID.")
    entity = state.entities.get(actor_id)
    if entity is None or entity.kind not in {"character", "person"} or entity.status != "active":
        raise InvalidEvent("Only an active character in combat can participate in a truce.")


def _validated_offers(
    records: object,
) -> list[CombatTruceOffer]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise InvalidEvent("Combat truce offers must be a sequence.")
    try:
        parsed = [_validated_offer(item) for item in records]
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("A combat truce offer record is invalid.") from exc
    ids = [item.offer_id for item in parsed]
    if len(ids) != len(set(ids)):
        raise InvalidEvent("Combat truce offer IDs must be unique.")
    return parsed


def _validated_offer(value: object) -> CombatTruceOffer:
    if isinstance(value, CombatTruceOffer):
        raw: object = value.model_dump(mode="python")
    else:
        raw = value
    return CombatTruceOffer.model_validate(raw)


def _require_state_and_scope(
    state: WorldState,
    *,
    operation: object,
    actor_id: object,
    world_id: object,
    branch_id: object,
    command_id: object,
    ruleset_version: object,
) -> None:
    _require_state(state)
    if operation not in {"offer", "accept", "decline"}:
        raise InvalidEvent("Combat truce operation is invalid.")
    if not all(isinstance(value, UUID) for value in (actor_id, world_id, branch_id, command_id)):
        raise InvalidEvent("Combat truce command, world, branch, and actor IDs must be UUIDs.")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip() or len(ruleset_version) > 100:
        raise InvalidEvent("Combat truce ruleset version must be nonempty and at most 100 characters.")


def _require_state(state: WorldState) -> None:
    from .models import WorldState

    if not isinstance(state, WorldState):
        raise InvalidEvent("A validated WorldState is required.")
