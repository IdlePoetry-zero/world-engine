"""Private, consent-based transfers of registered inventory between characters."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, TypeAdapter, model_validator

from .components import ExactDecimal, InventoryComponent, InventoryLot, ResourceName
from .errors import InsufficientResources, InvalidEvent
from .inventory import FifoLotAllocation, allocate_fifo_lots, reduce_fifo_inventory
from .json_types import JsonObject

if TYPE_CHECKING:
    from .models import ResourceAccount, WorldEntity, WorldState


PRIVATE_GIFT_CHANGED_EVENT_TYPE = "core.private_gift_changed.v1"
PrivateGiftOperation = Literal["offer", "accept", "decline", "withdraw"]
PrivateGiftStatus = Literal["pending", "accepted", "declined", "withdrawn"]
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
_MAX_REVISION = 2**63 - 1


class _PrivateGiftModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class PrivateGiftLotTransfer(_PrivateGiftModel):
    """One source lot and its exact recipient-side copy."""

    source_lot_id: UUID
    received_lot_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class PrivateGiftTransferEvidence(_PrivateGiftModel):
    """Transfer data re-derived from current accounts and FIFO inventory."""

    from_account_id: UUID
    to_account_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    source_balance_before: ExactDecimal = Field(ge=Decimal("0"))
    destination_balance_before: ExactDecimal = Field(ge=Decimal("0"))
    source_inventory_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    destination_inventory_revision: StrictInt | None = Field(default=None, ge=1, le=_MAX_REVISION)
    destination_account_created: StrictBool = False
    lots: tuple[PrivateGiftLotTransfer, ...] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def transfer_shape(self) -> PrivateGiftTransferEvidence:
        if self.from_account_id == self.to_account_id:
            raise ValueError("a private gift transfer needs distinct resource accounts")
        if self.destination_account_created and self.destination_balance_before != 0:
            raise ValueError("a new private gift recipient account must start at zero")
        if len({row.source_lot_id for row in self.lots}) != len(self.lots):
            raise ValueError("a private gift transfer cannot repeat a source lot")
        if len({row.received_lot_id for row in self.lots}) != len(self.lots):
            raise ValueError("a private gift transfer cannot repeat a received lot")
        with localcontext() as context:
            context.prec = 80
            lot_total = sum((row.quantity for row in self.lots), Decimal("0"))
        if lot_total != self.quantity:
            raise ValueError("private gift FIFO lots must sum to the transfer quantity")
        return self


class PrivateGiftOffer(_PrivateGiftModel):
    """One private proposal. It reserves no stock before explicit acceptance."""

    gift_id: UUID
    giver_id: UUID
    recipient_id: UUID
    place_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    offered_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    expires_at_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    status: PrivateGiftStatus = "pending"
    resolved_tick: StrictInt | None = Field(default=None, ge=_MIN_TICK, le=_MAX_TICK)
    source_account_id: UUID

    @model_validator(mode="after")
    def offer_lifecycle(self) -> PrivateGiftOffer:
        if self.giver_id == self.recipient_id:
            raise ValueError("a private gift requires two distinct characters")
        if self.expires_at_tick <= self.offered_tick:
            raise ValueError("a private gift must expire after it is offered")
        if self.status == "pending":
            if self.revision != 1 or self.resolved_tick is not None:
                raise ValueError("a pending private gift must remain at its first revision")
        elif self.revision != 2 or self.resolved_tick is None or self.resolved_tick < self.offered_tick:
            raise ValueError("a resolved private gift must have one committed response revision")
        return self


class PrivateGiftChangedEvent(_PrivateGiftModel):
    """One command-bound gift proposal or response, private to both participants."""

    type: Literal["core.private_gift_changed.v1"] = PRIVATE_GIFT_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    ruleset_version: str = Field(min_length=1, max_length=100)
    operation: PrivateGiftOperation
    expected_revision: StrictInt = Field(ge=0, le=_MAX_REVISION)
    offer: PrivateGiftOffer
    transfer: PrivateGiftTransferEvidence | None = Field(default=None, exclude_if=lambda value: value is None)
    visibility: JsonObject

    @model_validator(mode="after")
    def event_is_command_bound_and_private(self) -> PrivateGiftChangedEvent:
        offer = self.offer
        if self.event_id != private_gift_event_id(
            self.command_id, self.ruleset_version, self.operation, offer.gift_id,
        ):
            raise ValueError("private gift event ID must derive from its command, operation, and gift")
        if self.operation == "offer":
            if (
                offer.gift_id != private_gift_id(self.command_id)
                or self.expected_revision != 0
                or offer.revision != 1
                or offer.status != "pending"
                or offer.resolved_tick is not None
                or self.actor_id != offer.giver_id
                or self.requested_by is not None
                or self.transfer is not None
            ):
                raise ValueError("a private gift offer must be a command-bound pending proposal")
        else:
            statuses = {"accept": "accepted", "decline": "declined", "withdraw": "withdrawn"}
            if (
                self.expected_revision < 1
                or self.expected_revision >= _MAX_REVISION
                or offer.revision != self.expected_revision + 1
                or offer.status != statuses[self.operation]
                or offer.resolved_tick is None
            ):
                raise ValueError("private gift status and revision must match its response operation")
            expected_actor = offer.recipient_id if self.operation in {"accept", "decline"} else offer.giver_id
            if self.actor_id != expected_actor:
                raise ValueError("private gift response actor does not have authority for this transition")
            if (self.operation == "accept") != (self.transfer is not None):
                raise ValueError("only an accepted private gift carries transfer evidence")
            if self.requested_by is not None and (
                self.operation not in {"accept", "decline"}
                or self.requested_by != offer.giver_id
                or self.actor_id != offer.recipient_id
            ):
                raise ValueError("a hosted gift response must be requested by the giver for the recipient")
        if self.transfer is not None and (
            self.transfer.from_account_id != offer.source_account_id
            or self.transfer.resource_id != offer.resource_id
            or self.transfer.quantity != offer.quantity
        ):
            raise ValueError("private gift transfer must match the offered source, resource, and quantity")
        if self.visibility != private_gift_visibility(offer.giver_id, offer.recipient_id):
            raise ValueError("private gift details must be visible only to its two participants")
        return self


PRIVATE_GIFT_EVENT_ADAPTER = TypeAdapter(PrivateGiftChangedEvent)


def private_gift_id(command_id: UUID) -> UUID:
    if not isinstance(command_id, UUID):
        raise ValueError("private gift IDs require a command UUID")
    return uuid5(command_id, "private-gift")


def private_gift_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: PrivateGiftOperation,
    gift_id: UUID,
) -> UUID:
    if not isinstance(command_id, UUID) or not isinstance(gift_id, UUID):
        raise ValueError("private gift event IDs require command and gift UUIDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("private gift event IDs require a nonempty ruleset version")
    if operation not in {"offer", "accept", "decline", "withdraw"}:
        raise ValueError("private gift operation is unsupported")
    return uuid5(command_id, f"{ruleset_version}:private_gift.{operation}:{gift_id}")


def private_gift_recipient_account_id(gift_id: UUID) -> UUID:
    if not isinstance(gift_id, UUID):
        raise ValueError("private gift recipient account ID requires a gift UUID")
    return uuid5(gift_id, "private-gift.recipient-holder")


def private_gift_received_lot_id(event_id: UUID, source_lot_id: UUID) -> UUID:
    if not isinstance(event_id, UUID) or not isinstance(source_lot_id, UUID):
        raise ValueError("private gift received lot IDs require event and source-lot UUIDs")
    return uuid5(event_id, f"private-gift.received:{source_lot_id}")


def private_gift_visibility(giver_id: UUID, recipient_id: UUID) -> JsonObject:
    if not isinstance(giver_id, UUID) or not isinstance(recipient_id, UUID) or giver_id == recipient_id:
        raise ValueError("private gift visibility requires distinct participant UUIDs")
    return {"policy": "subject_ids", "subject_ids": sorted((str(giver_id), str(recipient_id)))}


def plan_private_gift_offer(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    recipient_id: UUID,
    resource_id: str,
    quantity: Decimal,
    expires_at_tick: int | None = None,
) -> PrivateGiftChangedEvent:
    """Plan a private gift offer without reserving or moving any resource."""

    _require_state(state)
    _validate_scope(world_id, branch_id, command_id, actor_id, ruleset_version)
    giver, recipient, place_id = _require_co_located_pair(state, actor_id, recipient_id)
    if any(row.gift_id == private_gift_id(command_id) for row in _validated_gifts(getattr(state, "private_gifts", ()))):
        raise InvalidEvent("Private gift ID already exists in the current state.")
    try:
        normalized_quantity = Decimal(quantity) if isinstance(quantity, (str, int, Decimal)) and not isinstance(quantity, bool) else None
        if normalized_quantity is None or not normalized_quantity.is_finite() or normalized_quantity <= 0:
            raise ValueError("quantity must be a positive exact decimal")
        provisional = PrivateGiftOffer(
            gift_id=private_gift_id(command_id),
            giver_id=giver.id,
            recipient_id=recipient.id,
            place_id=place_id,
            resource_id=resource_id,
            quantity=normalized_quantity,
            offered_tick=state.current_tick,
            expires_at_tick=_gift_expiry(state.current_tick, state.calendar.day_seconds, expires_at_tick),
            revision=1,
            status="pending",
            source_account_id=UUID(int=0),
        )
    except (ArithmeticError, TypeError, ValueError) as error:
        raise InvalidEvent("Private gift offer fields are invalid.") from error

    source_inventory = _inventory_for(giver, "giver")
    source_account_id, _source_account = _select_holder_account(
        state,
        giver.id,
        provisional.resource_id,
        quantity=provisional.quantity,
        at_tick=state.current_tick,
        inventory=source_inventory,
    )
    offer = provisional.model_copy(update={"source_account_id": source_account_id})
    return PrivateGiftChangedEvent(
        event_id=private_gift_event_id(command_id, ruleset_version, "offer", offer.gift_id),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
        operation="offer",
        expected_revision=0,
        offer=offer,
        visibility=private_gift_visibility(offer.giver_id, offer.recipient_id),
    )


def plan_private_gift_resolution(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    gift_id: UUID,
    expected_revision: int,
    operation: Literal["accept", "decline", "withdraw"],
    requested_by: UUID | None = None,
) -> PrivateGiftChangedEvent:
    """Plan one recipient response or giver withdrawal from current state."""

    _require_state(state)
    _validate_scope(world_id, branch_id, command_id, actor_id, ruleset_version)
    if operation not in {"accept", "decline", "withdraw"}:
        raise InvalidEvent("Private gift response operation is invalid.")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or not 1 <= expected_revision < _MAX_REVISION:
        raise InvalidEvent("Private gift expected revision is invalid.")
    offers = _validated_gifts(getattr(state, "private_gifts", ()))
    matches = [row for row in offers if row.gift_id == gift_id]
    if len(matches) != 1:
        raise InvalidEvent("Private gift does not exist exactly once in the current state.")
    current = matches[0]
    if current.revision != expected_revision or current.status != "pending":
        raise InvalidEvent("Private gift revision is stale or no longer pending.")
    expected_actor = current.recipient_id if operation in {"accept", "decline"} else current.giver_id
    if actor_id != expected_actor:
        raise InvalidEvent("Only the designated private gift participant may perform this response.")
    _require_active_character(state, actor_id)
    if requested_by is not None:
        if operation not in {"accept", "decline"} or requested_by != current.giver_id or actor_id != current.recipient_id:
            raise InvalidEvent("A hosted gift response must be requested by the giver for the recipient.")
        if current.expires_at_tick <= state.current_tick:
            raise InvalidEvent("A hosted response requires an unexpired private gift.")
        _require_offer_pair_at_place(state, current)

    status: PrivateGiftStatus = {"accept": "accepted", "decline": "declined", "withdraw": "withdrawn"}[operation]
    if operation == "accept":
        if current.expires_at_tick <= state.current_tick:
            raise InvalidEvent("An expired private gift cannot be accepted.")
        _require_offer_pair_at_place(state, current)

    resulting_offer = PrivateGiftOffer(
        **current.model_dump(mode="python") | {
            "status": status,
            "revision": current.revision + 1,
            "resolved_tick": state.current_tick,
        }
    )
    transfer = (
        _plan_private_gift_transfer(state, current, command_id, ruleset_version)
        if operation == "accept"
        else None
    )
    return PrivateGiftChangedEvent(
        event_id=private_gift_event_id(command_id, ruleset_version, operation, current.gift_id),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        requested_by=requested_by,
        ruleset_version=ruleset_version,
        operation=operation,
        expected_revision=expected_revision,
        offer=resulting_offer,
        transfer=transfer,
        visibility=private_gift_visibility(current.giver_id, current.recipient_id),
    )


def apply_private_gift(state: WorldState, event: PrivateGiftChangedEvent) -> dict[str, object]:
    """Re-derive a gift event from the current state and return reducer updates."""

    _require_state(state)
    try:
        typed_event = PRIVATE_GIFT_EVENT_ADAPTER.validate_python(event)
        if typed_event.operation == "offer":
            expected = plan_private_gift_offer(
                state,
                world_id=typed_event.world_id,
                branch_id=typed_event.branch_id,
                command_id=typed_event.command_id,
                actor_id=typed_event.actor_id,
                ruleset_version=typed_event.ruleset_version,
                recipient_id=typed_event.offer.recipient_id,
                resource_id=typed_event.offer.resource_id,
                quantity=typed_event.offer.quantity,
                expires_at_tick=typed_event.offer.expires_at_tick,
            )
        else:
            expected = plan_private_gift_resolution(
                state,
                world_id=typed_event.world_id,
                branch_id=typed_event.branch_id,
                command_id=typed_event.command_id,
                actor_id=typed_event.actor_id,
                ruleset_version=typed_event.ruleset_version,
                gift_id=typed_event.offer.gift_id,
                expected_revision=typed_event.expected_revision,
                operation=typed_event.operation,
                requested_by=typed_event.requested_by,
            )
    except InvalidEvent:
        raise
    except Exception as error:
        raise InvalidEvent("Private gift event is malformed or cannot be re-derived.") from error
    if typed_event != expected:
        if typed_event.transfer is not None and expected.transfer is not None:
            if typed_event.transfer.source_inventory_revision != expected.transfer.source_inventory_revision:
                raise InvalidEvent("Private gift source inventory revision compare-and-swap failed.")
            if typed_event.transfer.destination_inventory_revision != expected.transfer.destination_inventory_revision:
                raise InvalidEvent("Private gift destination inventory revision compare-and-swap failed.")
        raise InvalidEvent("Private gift event does not match its authoritative transition.")

    offers = _validated_gifts(getattr(state, "private_gifts", ()))
    if typed_event.operation == "offer":
        offers.append(typed_event.offer)
    else:
        offers = [typed_event.offer if row.gift_id == typed_event.offer.gift_id else row for row in offers]
    updates: dict[str, object] = {"private_gifts": offers}
    if typed_event.transfer is not None:
        updates.update(_apply_private_gift_transfer(state, typed_event))
    return updates


def _plan_private_gift_transfer(
    state: WorldState,
    offer: PrivateGiftOffer,
    command_id: UUID,
    ruleset_version: str,
) -> PrivateGiftTransferEvidence:
    giver = _require_active_character(state, offer.giver_id)
    recipient = _require_active_character(state, offer.recipient_id)
    _require_offer_pair_at_place(state, offer)
    source_account = _require_holder_account(state, offer.source_account_id, offer.giver_id, offer.resource_id)
    if source_account.balance < offer.quantity:
        raise InsufficientResources("The giver's registered holder account no longer covers the gift.")
    source_inventory = _inventory_for(giver, "giver")
    recipient_inventory = _inventory_for(recipient, "recipient", allow_missing=True)
    allocations = _allocate_fifo(
        source_inventory,
        offer.source_account_id,
        offer.resource_id,
        offer.quantity,
        state.current_tick,
    )
    if len(recipient_inventory.lots) + len(allocations) > 100_000:
        raise InvalidEvent("The recipient inventory cannot hold all of the exact private gift lots.")
    to_account_id, destination_account, created = _destination_account(state, recipient.id, offer)
    destination_balance = destination_account.balance if destination_account is not None else Decimal("0")

    event_id = private_gift_event_id(command_id, ruleset_version, "accept", offer.gift_id)
    received_lots = [
        PrivateGiftLotTransfer(
            source_lot_id=allocation.lot_id,
            received_lot_id=private_gift_received_lot_id(event_id, allocation.lot_id),
            quantity=allocation.quantity,
        )
        for allocation in allocations
    ]
    all_existing_lot_ids = {
        lot.lot_id
        for entity in state.entities.values()
        for raw in [entity.components.get("inventory")]
        if raw is not None
        for lot in InventoryComponent.model_validate(raw).lots
    }
    if any(row.received_lot_id in all_existing_lot_ids for row in received_lots):
        raise InvalidEvent("A deterministic private gift received lot ID already exists in inventory.")
    if len({row.received_lot_id for row in received_lots}) != len(received_lots):
        raise InvalidEvent("A private gift transfer cannot repeat a received lot ID.")
    with localcontext() as context:
        context.prec = 80
        if destination_balance + offer.quantity > Decimal("9" * 30 + ".999999"):
            raise InvalidEvent("The recipient account would exceed supported resource precision.")
    return PrivateGiftTransferEvidence(
        from_account_id=offer.source_account_id,
        to_account_id=to_account_id,
        resource_id=offer.resource_id,
        quantity=offer.quantity,
        source_balance_before=source_account.balance,
        destination_balance_before=destination_balance,
        source_inventory_revision=source_inventory.revision,
        destination_inventory_revision=(
            recipient_inventory.revision if recipient.components.get("inventory") is not None else None
        ),
        destination_account_created=created,
        lots=tuple(received_lots),
    )


def _apply_private_gift_transfer(state: WorldState, event: PrivateGiftChangedEvent) -> dict[str, object]:
    from .models import ResourceAccount as RuntimeResourceAccount

    transfer = event.transfer
    if transfer is None:
        raise InvalidEvent("Accepted private gift is missing transfer evidence.")
    offer = event.offer
    giver = _require_active_character(state, offer.giver_id)
    recipient = _require_active_character(state, offer.recipient_id)
    _require_offer_pair_at_place(state, offer)
    source_account = _require_holder_account(state, transfer.from_account_id, offer.giver_id, offer.resource_id)
    if source_account.balance != transfer.source_balance_before or source_account.balance < offer.quantity:
        raise InvalidEvent("Private gift source balance compare-and-swap failed.")
    source_inventory = _inventory_for(giver, "giver")
    recipient_inventory = _inventory_for(recipient, "recipient", allow_missing=True)
    if source_inventory.revision != transfer.source_inventory_revision:
        raise InvalidEvent("Private gift source inventory revision compare-and-swap failed.")
    current_recipient_revision = (
        recipient_inventory.revision if recipient.components.get("inventory") is not None else None
    )
    if current_recipient_revision != transfer.destination_inventory_revision:
        raise InvalidEvent("Private gift destination inventory revision compare-and-swap failed.")
    allocations = _allocate_fifo(
        source_inventory,
        transfer.from_account_id,
        offer.resource_id,
        offer.quantity,
        state.current_tick,
    )
    if tuple(
        (allocation.lot_id, allocation.quantity, private_gift_received_lot_id(event.event_id, allocation.lot_id))
        for allocation in allocations
    ) != tuple((row.source_lot_id, row.quantity, row.received_lot_id) for row in transfer.lots):
        raise InvalidEvent("Private gift transfer manifest does not match exact FIFO allocation.")

    target_raw = state.resources.get(transfer.to_account_id)
    if transfer.destination_account_created:
        if target_raw is not None or transfer.to_account_id != private_gift_recipient_account_id(offer.gift_id):
            raise InvalidEvent("Private gift recipient account creation does not match its deterministic account ID.")
        destination_account = RuntimeResourceAccount(
            resource_id=offer.resource_id,
            balance=Decimal("0"),
            owner_id=offer.recipient_id,
        )
    else:
        destination_account = _require_holder_account(
            state, transfer.to_account_id, offer.recipient_id, offer.resource_id,
        )
    if destination_account.balance != transfer.destination_balance_before:
        raise InvalidEvent("Private gift destination balance compare-and-swap failed.")
    try:
        reduced_source = reduce_fifo_inventory(source_inventory, allocations)
        source_lots = {lot.lot_id: lot for lot in source_inventory.lots}
        received_lots = [
            InventoryLot(
                lot_id=row.received_lot_id,
                account_id=transfer.to_account_id,
                resource_id=offer.resource_id,
                quantity=row.quantity,
                quality=source_lots[row.source_lot_id].quality,
                expiry_tick=source_lots[row.source_lot_id].expiry_tick,
                origin_event_id=source_lots[row.source_lot_id].origin_event_id,
            )
            for row in transfer.lots
        ]
        existing_recipient_lot_ids = {lot.lot_id for lot in recipient_inventory.lots}
        if any(lot.lot_id in existing_recipient_lot_ids for lot in received_lots):
            raise InvalidEvent("Private gift received lot ID already exists in recipient inventory.")
        updated_source_inventory = reduced_source
        updated_recipient_inventory = InventoryComponent(
            revision=(
                recipient_inventory.revision + 1
                if recipient.components.get("inventory") is not None else 1
            ),
            lots=[*recipient_inventory.lots, *received_lots],
        )
        with localcontext() as context:
            context.prec = 80
            updated_source_account = RuntimeResourceAccount.model_validate({
                **source_account.model_dump(mode="python"),
                "balance": source_account.balance - offer.quantity,
            })
            updated_destination_account = RuntimeResourceAccount.model_validate({
                **destination_account.model_dump(mode="python"),
                "balance": destination_account.balance + offer.quantity,
            })
    except (InvalidEvent, InsufficientResources):
        raise
    except Exception as error:
        raise InvalidEvent("Private gift transfer would violate inventory or account bounds.") from error

    entities = dict(state.entities)
    entities[giver.id] = giver.model_copy(update={
        "components": {**giver.components, "inventory": updated_source_inventory.model_dump(mode="json")},
    })
    entities[recipient.id] = recipient.model_copy(update={
        "components": {**recipient.components, "inventory": updated_recipient_inventory.model_dump(mode="json")},
    })
    resources = dict(state.resources)
    resources[transfer.from_account_id] = updated_source_account
    resources[transfer.to_account_id] = updated_destination_account
    return {"entities": entities, "resources": resources}


def _allocate_fifo(
    inventory: InventoryComponent,
    account_id: UUID,
    resource_id: str,
    quantity: Decimal,
    at_tick: int,
) -> list[FifoLotAllocation]:
    try:
        return allocate_fifo_lots(
            inventory,
            resource_id=resource_id,
            quantity=quantity,
            at_tick=at_tick,
            account_id=account_id,
        )
    except ValueError as error:
        raise InsufficientResources("Unexpired FIFO inventory does not cover the private gift.") from error


def _inventory_for(entity: WorldEntity, label: str, *, allow_missing: bool = False) -> InventoryComponent:
    raw = entity.components.get("inventory")
    if raw is None:
        if allow_missing:
            return InventoryComponent()
        raise InvalidEvent(f"The {label} must have a valid registered inventory.")
    try:
        return InventoryComponent.model_validate(raw)
    except Exception as error:
        raise InvalidEvent(f"The {label} inventory is invalid.") from error


def _select_holder_account(
    state: WorldState,
    owner_id: UUID,
    resource_id: str,
    *,
    quantity: Decimal,
    at_tick: int,
    inventory: InventoryComponent,
) -> tuple[UUID, ResourceAccount]:
    candidates = sorted(
        (
            (account_id, account)
            for account_id, raw in state.resources.items()
            for account in [_parse_account(raw)]
            if account.owner_id == owner_id
            and account.account_type == "holder"
            and account.resource_id == resource_id
        ),
        key=lambda row: str(row[0]),
    )
    if not candidates:
        raise InvalidEvent("The giver has no registered holder account for this resource.")
    for account_id, account in candidates:
        if account.balance < quantity:
            continue
        try:
            allocate_fifo_lots(
                inventory,
                resource_id=resource_id,
                quantity=quantity,
                at_tick=at_tick,
                account_id=account_id,
            )
        except ValueError:
            continue
        return account_id, account
    raise InsufficientResources("No giver holder account and unexpired FIFO lots cover the private gift.")


def _destination_account(
    state: WorldState,
    recipient_id: UUID,
    offer: PrivateGiftOffer,
) -> tuple[UUID, ResourceAccount | None, bool]:
    candidates = sorted(
        (
            (account_id, account)
            for account_id, raw in state.resources.items()
            for account in [_parse_account(raw)]
            if account.owner_id == recipient_id
            and account.account_type == "holder"
            and account.resource_id == offer.resource_id
        ),
        key=lambda row: str(row[0]),
    )
    if candidates:
        return candidates[0][0], candidates[0][1], False
    account_id = private_gift_recipient_account_id(offer.gift_id)
    if account_id in state.resources:
        raise InvalidEvent("The deterministic private gift recipient account ID is already occupied.")
    return account_id, None, True


def _parse_account(raw: object) -> ResourceAccount:
    from .models import ResourceAccount as RuntimeResourceAccount

    try:
        if isinstance(raw, RuntimeResourceAccount):
            return RuntimeResourceAccount.model_validate(raw.model_dump(mode="python"))
        return RuntimeResourceAccount.model_validate(raw)
    except Exception as error:
        raise InvalidEvent("Private gift resource account is invalid.") from error


def _require_holder_account(
    state: WorldState,
    account_id: UUID,
    owner_id: UUID,
    resource_id: str,
) -> ResourceAccount:
    raw = state.resources.get(account_id)
    if raw is None:
        raise InvalidEvent("Private gift holder account is unavailable.")
    account = _parse_account(raw)
    if (
        account.account_type != "holder"
        or account.owner_id != owner_id
        or account.resource_id != resource_id
    ):
        raise InvalidEvent("Private gift holder account does not match its owner and resource.")
    return account


def _require_co_located_pair(state: WorldState, giver_id: UUID, recipient_id: UUID) -> tuple[WorldEntity, WorldEntity, UUID]:
    giver = _require_active_character(state, giver_id)
    recipient = _require_active_character(state, recipient_id)
    if giver.id == recipient.id or giver.location_id is None or giver.location_id != recipient.location_id:
        raise InvalidEvent("A private gift requires two distinct characters at the same registered place.")
    place = state.entities.get(giver.location_id)
    if place is None or place.kind not in {"place", "location"} or place.status != "active":
        raise InvalidEvent("A private gift requires a registered active place.")
    return giver, recipient, place.id


def _require_offer_pair_at_place(state: WorldState, offer: PrivateGiftOffer) -> None:
    giver, recipient, place_id = _require_co_located_pair(state, offer.giver_id, offer.recipient_id)
    if place_id != offer.place_id:
        raise InvalidEvent("The private gift participants are no longer at the offered place.")


def _require_active_character(state: WorldState, actor_id: UUID) -> WorldEntity:
    entity = state.entities.get(actor_id)
    if entity is None or entity.kind not in {"character", "person"} or entity.status != "active":
        raise InvalidEvent("Private gifts require an active character.")
    return entity


def _gift_expiry(current_tick: int, day_seconds: int, requested: int | None) -> int:
    if requested is not None:
        if not isinstance(requested, int) or isinstance(requested, bool) or not _MIN_TICK <= requested <= _MAX_TICK:
            raise InvalidEvent("Private gift expiration must be a signed world tick.")
        if requested <= current_tick:
            raise InvalidEvent("Private gift expiration must be in the future.")
        return requested
    if not isinstance(day_seconds, int) or isinstance(day_seconds, bool) or day_seconds <= 0:
        raise InvalidEvent("The world calendar must have a positive day length.")
    return min(_MAX_TICK, current_tick + 7 * day_seconds)


def _validate_scope(
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
) -> None:
    if any(not isinstance(value, UUID) for value in (world_id, branch_id, command_id, actor_id)):
        raise InvalidEvent("Private gift command scope requires UUID values.")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip() or len(ruleset_version) > 100:
        raise InvalidEvent("Private gift command scope requires a registered ruleset version.")


def _validated_gifts(records: object) -> list[PrivateGiftOffer]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise InvalidEvent("Private gift state must be a sequence of typed offers.")
    try:
        gifts = [
            PrivateGiftOffer.model_validate(row.model_dump(mode="python") if isinstance(row, PrivateGiftOffer) else row)
            for row in records
        ]
    except Exception as error:
        raise InvalidEvent("Private gift state contains an invalid offer.") from error
    ids = [row.gift_id for row in gifts]
    if len(ids) != len(set(ids)):
        raise InvalidEvent("Private gift IDs must be unique.")
    return gifts


def _require_state(state: WorldState) -> None:
    from .models import WorldState as RuntimeWorldState

    if not isinstance(state, RuntimeWorldState):
        raise InvalidEvent("A validated WorldState is required for private gifts.")
