from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import InventoryComponent, InventoryLot, MarketComponent, MarketOffer
from world_engine.domain.gameplay_events import MarketTradeExecutedEvent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules.contracts import RuleContext, TradeIntent, TradePriceCeiling, parse_action
from world_engine.rules.engine import RuleEngine


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
BUYER_ID = UUID("30000000-0000-4000-8000-000000000003")
SELLER_ID = UUID("40000000-0000-4000-8000-000000000004")
MARKET_ID = UUID("50000000-0000-4000-8000-000000000005")
LISTING_ID = UUID("60000000-0000-4000-8000-000000000006")
STOCK_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")
BUYER_CURRENCY_ACCOUNT_ID = UUID("80000000-0000-4000-8000-000000000008")
SELLER_CURRENCY_ACCOUNT_ID = UUID("90000000-0000-4000-8000-000000000009")
BUYER_GOODS_ACCOUNT_ID = UUID("a0000000-0000-4000-8000-00000000000a")
STOCK_LOT_ID = UUID("b0000000-0000-4000-8000-00000000000b")
CURRENCY_ID = "core.coin"
GOODS_ID = "core.ore"
RULESET = "market-price-ceiling-test.v1"


def _state(*, unit_price_minor: int = 5) -> WorldState:
    return WorldState(
        entities={
            BUYER_ID: WorldEntity(id=BUYER_ID, kind="character", status="active", location_id=MARKET_ID),
            SELLER_ID: WorldEntity(
                id=SELLER_ID,
                kind="organization",
                status="active",
                location_id=MARKET_ID,
                components={"inventory": InventoryComponent(lots=[InventoryLot(
                    lot_id=STOCK_LOT_ID,
                    account_id=STOCK_ACCOUNT_ID,
                    resource_id=GOODS_ID,
                    quantity="3",
                )]).model_dump(mode="json")},
            ),
            MARKET_ID: WorldEntity(
                id=MARKET_ID,
                kind="place",
                status="active",
                components={"market": MarketComponent(
                    currency_resource_id=CURRENCY_ID,
                    offers=[MarketOffer(
                        listing_id=LISTING_ID,
                        resource_id=GOODS_ID,
                        stock_account_id=STOCK_ACCOUNT_ID,
                        seller_id=SELLER_ID,
                        unit_price_minor=unit_price_minor,
                        minimum_quantity="1",
                    )],
                ).model_dump(mode="json")},
            ),
        },
        resources={
            STOCK_ACCOUNT_ID: ResourceAccount(resource_id=GOODS_ID, balance="3", owner_id=SELLER_ID),
            BUYER_CURRENCY_ACCOUNT_ID: ResourceAccount(
                resource_id=CURRENCY_ID, balance="100", owner_id=BUYER_ID,
            ),
            SELLER_CURRENCY_ACCOUNT_ID: ResourceAccount(
                resource_id=CURRENCY_ID, balance="10", owner_id=SELLER_ID,
            ),
            BUYER_GOODS_ACCOUNT_ID: ResourceAccount(resource_id=GOODS_ID, balance="0", owner_id=BUYER_ID),
        },
    )


def _intent(*, ceiling: int | None = 10, currency: str = CURRENCY_ID) -> TradeIntent:
    payload: dict[str, object] = {"kind": "trade", "listing_id": LISTING_ID, "quantity": "2"}
    if ceiling is not None:
        payload["price_ceiling"] = {"max_total_minor": ceiling, "currency_resource_id": currency}
    return TradeIntent.model_validate(payload)


def _evaluate(state: WorldState, intent: TradeIntent):
    return RuleEngine().evaluate(
        state,
        intent,
        RuleContext(
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uuid4(),
            actor_id=BUYER_ID,
            ruleset_version=RULESET,
        ),
    )


@pytest.mark.parametrize(
    ("unit_price_minor", "ceiling", "expected_status", "expected_code"),
    [
        (4, 10, "succeeded", "trade_completed"),
        (5, 10, "succeeded", "trade_completed"),
        (6, 10, "rejected", "price_ceiling_exceeded"),
    ],
)
def test_total_price_ceiling_compares_current_whole_transaction_payment(
    unit_price_minor: int,
    ceiling: int,
    expected_status: str,
    expected_code: str,
):
    state = _state(unit_price_minor=unit_price_minor)
    before_hash = state_hash(state)
    result = _evaluate(state, _intent(ceiling=ceiling))

    assert result.outcome.status == expected_status
    assert result.outcome.code == expected_code
    if expected_status == "rejected":
        assert result.elapsed_ticks == 0
        assert result.events == []
        assert state_hash(state) == before_hash
    else:
        trade, = [event for event in result.events if isinstance(event, MarketTradeExecutedEvent)]
        assert trade.quantity * trade.unit_price_minor <= ceiling
        assert trade.currency_resource_id == CURRENCY_ID


def test_ceiling_equality_executes_real_market_trade_and_posts_balanced_resource_changes():
    state = _state(unit_price_minor=5)
    result = _evaluate(state, _intent(ceiling=10))
    assert result.outcome.status == "succeeded"
    trade, = [event for event in result.events if isinstance(event, MarketTradeExecutedEvent)]
    assert trade.quantity * trade.unit_price_minor == Decimal("10")

    settled = state
    for event in result.events:
        settled = apply_event(settled, event)

    assert settled.resources[BUYER_CURRENCY_ACCOUNT_ID].balance == Decimal("90")
    assert settled.resources[SELLER_CURRENCY_ACCOUNT_ID].balance == Decimal("20")
    assert settled.resources[BUYER_GOODS_ACCOUNT_ID].balance == Decimal("2")
    assert settled.resources[STOCK_ACCOUNT_ID].balance == Decimal("1")


def test_ceiling_rejects_a_changed_market_quote_using_current_currency_and_price():
    state = _state(unit_price_minor=6)
    before_hash = state_hash(state)
    result = _evaluate(state, _intent(ceiling=10, currency=CURRENCY_ID))

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "price_ceiling_exceeded"
    assert result.elapsed_ticks == 0
    assert result.events == []
    assert state_hash(state) == before_hash


def test_ceiling_requires_the_market_current_currency_identity():
    state = _state()
    before_hash = state_hash(state)
    result = _evaluate(state, _intent(ceiling=100, currency="core.silver"))

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "price_ceiling_currency_mismatch"
    assert result.elapsed_ticks == 0
    assert result.events == []
    assert state_hash(state) == before_hash


def test_old_trade_intent_serialization_omits_the_optional_ceiling():
    legacy = _intent(ceiling=None)
    serialized = legacy.model_dump(mode="json")

    assert "price_ceiling" not in serialized
    assert parse_action(serialized) == legacy


@pytest.mark.parametrize("amount", [True, 1.5, "10", 10**30])
def test_price_ceiling_amount_is_a_bounded_strict_minor_unit_integer(amount):
    with pytest.raises(ValidationError):
        TradePriceCeiling(max_total_minor=amount, currency_resource_id=CURRENCY_ID)
