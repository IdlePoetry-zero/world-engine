"""Read-only quotes used by durable work purchase targets."""

from __future__ import annotations

from decimal import Decimal, localcontext

from world_engine.domain.components import MarketComponent
from world_engine.domain.market_pricing import MarketPriceInputError, observe_market_offer
from world_engine.domain.models import WorldState

from .contracts import WorkPurchaseTarget


def observe_work_purchase_quote(state: WorldState, target: WorkPurchaseTarget) -> Decimal:
    """Return the current total listing price for the requested quantity.

    The observation uses the same active listing checks and eligible FIFO
    inventory calculation as trade pricing. It never reserves stock or buys it.
    """

    market_entity = state.entities.get(target.market_id)
    if market_entity is None:
        raise ValueError("purchase target market does not exist")
    try:
        market = MarketComponent.model_validate(market_entity.components.get("market"))
    except Exception as exc:
        raise ValueError("purchase target market has no valid market component") from exc
    if market.currency_resource_id != target.currency_resource_id:
        raise ValueError("purchase target currency does not match the market currency")

    offer = next((item for item in market.offers if item.listing_id == target.listing_id), None)
    if offer is None:
        raise ValueError("purchase target listing does not exist")
    if (
        offer.seller_id != target.seller_id
        or offer.resource_id != target.resource_id
    ):
        raise ValueError("purchase target identity does not match the current listing")
    if offer.minimum_quantity > target.quantity:
        raise ValueError("purchase target quantity is below the listing minimum")

    try:
        observation = observe_market_offer(
            state, target.market_id, target.listing_id, at_tick=state.current_tick,
        )
    except MarketPriceInputError as exc:
        raise ValueError("purchase target listing is currently unavailable") from exc
    if (
        observation.seller_id != target.seller_id
        or observation.resource_id != target.resource_id
        or observation.tradable_quantity < target.quantity
    ):
        raise ValueError("purchase target listing does not have the requested quantity available")

    with localcontext() as decimal_context:
        decimal_context.prec = 80
        total = Decimal(observation.current_unit_price_minor) * target.quantity
    if total != total.to_integral_value():
        raise ValueError("purchase target price does not resolve to an integer minor-unit payment")
    return total
