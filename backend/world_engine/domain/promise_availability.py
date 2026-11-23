"""Observe return-promise fulfillment conditions without changing the world."""

from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import ValidationError

from .components import InventoryComponent
from .errors import InvalidEvent
from .inventory import FifoLotAllocation, allocate_fifo_lots
from .relationship_promises import RelationshipPromise

if TYPE_CHECKING:
    from .models import ResourceAccount, WorldEntity, WorldState


def assess_return_promise(
    state: WorldState,
    actor: WorldEntity,
    promise: RelationshipPromise,
    *,
    visible_counterpart_ids: Collection[UUID],
) -> tuple[Literal["recipient_available_here", "not_observed_here", "recipient_account_unavailable"], bool]:
    """Return observable delivery feasibility and whether the actor has eligible stock.

    The caller must already have an active promise issued by this actor.
    No recipient whereabouts or status are inferred outside the actor's view.
    """
    stock = first_promise_stock_account(
        state, actor, resource_id=promise.terms.resource_id, quantity=promise.terms.quantity,
    )
    has_stock = stock is not None
    recipient = state.entities.get(promise.promisee_id)
    if (recipient is None or recipient.id not in visible_counterpart_ids or recipient.status != "active"
            or actor.location_id is None or actor.location_id != recipient.location_id):
        return "not_observed_here", has_stock
    account = state.resources.get(promise.terms.promisee_account_id)
    if not is_promise_holder_account(account, owner_id=promise.promisee_id, resource_id=promise.terms.resource_id):
        return "recipient_account_unavailable", has_stock
    with localcontext() as context:
        context.prec = 80
        if account.balance + promise.terms.quantity >= Decimal("1e30"):
            return "recipient_account_unavailable", has_stock
    recipient_inventory = promise_inventory_or_none(recipient)
    if stock is not None and recipient_inventory is not None and len(recipient_inventory.lots) + len(stock[3]) > 100_000:
        return "recipient_account_unavailable", has_stock
    return "recipient_available_here", has_stock


def first_promise_stock_account(
    state: WorldState,
    actor: WorldEntity,
    *,
    resource_id: str,
    quantity: Decimal,
) -> tuple[UUID, ResourceAccount, InventoryComponent, list[FifoLotAllocation]] | None:
    inventory = promise_inventory_or_none(actor)
    if inventory is None:
        return None
    candidates = sorted(
        (
            (account_id, account)
            for account_id, account in state.resources.items()
            if is_promise_holder_account(account, owner_id=actor.id, resource_id=resource_id)
            and account.balance >= quantity
        ),
        key=lambda pair: str(pair[0]),
    )
    for account_id, account in candidates:
        try:
            allocations = allocate_fifo_lots(
                inventory,
                resource_id=resource_id,
                quantity=quantity,
                at_tick=state.current_tick,
                account_id=account_id,
            )
        except ValueError:
            continue
        return account_id, account, inventory, allocations
    return None


def is_promise_holder_account(account: ResourceAccount | None, *, owner_id: UUID, resource_id: str) -> bool:
    return (
        account is not None
        and account.account_type == "holder"
        and account.owner_id == owner_id
        and account.resource_id == resource_id
    )


def promise_inventory_or_none(entity: WorldEntity) -> InventoryComponent | None:
    raw_inventory = entity.components.get("inventory")
    if raw_inventory is None:
        return None
    try:
        return InventoryComponent.model_validate(raw_inventory)
    except ValidationError as exc:
        raise InvalidEvent("Promise participant has invalid registered inventory.") from exc


