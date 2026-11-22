from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import (
    INTENT_SYSTEM_PROMPT,
    ConversationApplication,
    ConversationStore,
)
from world_engine.application.operations import OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import InventoryComponent, InventoryLot, MarketComponent, MarketOffer
from world_engine.domain.events import parse_event
from world_engine.domain.models import AccessScope, NewWorld, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import ledger_postings
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


CURRENCY_ID = "modern.credit"
GOODS_ID = "resource.ore"
UNIT_PRICE_MINOR = 5
QUANTITY = "2"


class StubModelOperations:
    """Return deterministic classifier/action proposals without any provider calls."""

    def __init__(self, listing_id, *, proposed_ceiling=None):
        self.listing_id = listing_id
        self.proposed_ceiling = proposed_ceiling
        self.calls = []

    async def generate(self, scope, request):
        assert request.role_task is RoleTask.INTENT
        self.calls.append(request)
        first_message = request.messages[0].content
        if first_message == INTENT_SYSTEM_PROMPT:
            output = {"intent": "action", "explanation": "玩家明确要求执行购买。"}
        else:
            action = {"kind": "trade", "listing_id": str(self.listing_id), "quantity": QUANTITY}
            if self.proposed_ceiling is not None:
                action["price_ceiling"] = self.proposed_ceiling
            output = {"action": action, "clarification": ""}
        return SimpleNamespace(normalized_output=output)


@pytest.fixture
def market_conversation(tmp_path):
    identity = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, seller_id, market_id = uuid4(), uuid4(), uuid4()
    listing_id, stock_id = uuid4(), uuid4()
    buyer_money_id, seller_money_id, buyer_goods_id = uuid4(), uuid4(), uuid4()
    stock_lot_id = uuid4()

    state = WorldState(
        player_id=player_id,
        entities={
            player_id: WorldEntity(
                id=player_id, kind="character", name="Player", location_id=market_id,
            ),
            seller_id: WorldEntity(
                id=seller_id,
                kind="organization",
                name="Ore Seller",
                location_id=market_id,
                components={"inventory": InventoryComponent(lots=[InventoryLot(
                    lot_id=stock_lot_id,
                    account_id=stock_id,
                    resource_id=GOODS_ID,
                    quantity="3",
                )]).model_dump(mode="json")},
            ),
            market_id: WorldEntity(
                id=market_id,
                kind="place",
                name="Town Market",
                components={"market": MarketComponent(
                    currency_resource_id=CURRENCY_ID,
                    offers=[MarketOffer(
                        listing_id=listing_id,
                        resource_id=GOODS_ID,
                        stock_account_id=stock_id,
                        seller_id=seller_id,
                        unit_price_minor=UNIT_PRICE_MINOR,
                        minimum_quantity="1",
                    )],
                ).model_dump(mode="json")},
            ),
        },
        resources={
            stock_id: ResourceAccount(resource_id=GOODS_ID, balance="3", owner_id=seller_id),
            buyer_money_id: ResourceAccount(resource_id=CURRENCY_ID, balance="100", owner_id=player_id),
            seller_money_id: ResourceAccount(resource_id=CURRENCY_ID, balance="10", owner_id=seller_id),
            buyer_goods_id: ResourceAccount(resource_id=GOODS_ID, balance="0", owner_id=player_id),
        },
    )

    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(identity, NewWorld(title="Market price ceiling", state=state))
    operations = OperationsStore(tmp_path / "operations.db")
    models = StubModelOperations(listing_id)
    service = ConversationApplication(WorldApplication(repository, None), models, ConversationStore(operations))
    service.actions = RulesActionDispatcher(
        repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
    )

    data = SimpleNamespace(
        identity=identity,
        repository=repository,
        database=database,
        service=service,
        models=models,
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        player_id=player_id,
        listing_id=listing_id,
        account_ids={
            "buyer_currency": buyer_money_id,
            "seller_currency": seller_money_id,
            "buyer_goods": buyer_goods_id,
            "stock": stock_id,
        },
    )
    yield data
    database.dispose()


def _request(data, *, text, mode="action", request_id=None):
    from world_engine.api.contracts import MessageRequest

    return MessageRequest(
        request_id=request_id or uuid4(),
        world_id=data.world_id,
        branch_id=data.branch_id,
        session_id=uuid4(),
        actor_id=data.player_id,
        expected_state_version="0",
        mode=mode,
        text=text,
    )


def _snapshot(data):
    return data.repository.read_world(data.identity, data.world_id, data.branch_id)


def _assert_replay_matches_head(data):
    head = _snapshot(data)
    replayed = data.repository.replay(
        data.identity, data.world_id, data.branch_id, from_genesis=True,
    )
    assert replayed.state_hash == head.state_hash
    assert replayed.state.current_tick == head.state.current_tick
    assert replayed.branch.state_version == head.branch.state_version
    assert replayed.last_seq == head.last_seq


@pytest.mark.asyncio
async def test_auto_purchase_cannot_exceed_written_total_ceiling_even_when_model_omits_it(market_conversation):
    data = market_conversation
    before = _snapshot(data)
    request = _request(data, mode="auto", text="总价不超过9信用点就买两份矿石。")

    result = await data.service.send(data.identity, request)

    assert result["kind"] == "action", result
    assert result["action_status"] == "rejected"
    assert result["world_effect"] is False
    assert len(data.models.calls) == 2  # auto classifier plus action proposal
    after = _snapshot(data)
    assert after.state_hash == before.state_hash
    assert after.state.current_tick == before.state.current_tick
    assert after.branch.state_version == before.branch.state_version
    assert after.last_seq == before.last_seq
    for account_id in data.account_ids.values():
        assert after.state.resources[account_id].balance == before.state.resources[account_id].balance

    # The same request is served from the durable conversation receipt, without
    # another classifier/action proposal or a second attempt to trade.
    assert await data.service.send(data.identity, request) == result
    assert len(data.models.calls) == 2
    _assert_replay_matches_head(data)


@pytest.mark.asyncio
async def test_explicit_price_ceiling_allows_equality_and_persists_payment_ledger(market_conversation):
    data = market_conversation
    before = _snapshot(data)
    request = _request(data, text="总价不超过10信用点就买两份矿石。")

    result = await data.service.send(data.identity, request)

    assert result["kind"] == "action", result
    assert result["action_status"] == "succeeded"
    assert result["world_effect"] is True
    assert len(data.models.calls) == 1  # explicit action skips classification
    after = _snapshot(data)
    assert after.branch.state_version == before.branch.state_version + 1
    committed_events = data.repository.list_events(
        data.identity, data.world_id, data.branch_id, after_seq=before.last_seq,
    ).events
    assert after.last_seq == before.last_seq + len(committed_events)
    assert after.state.current_tick == before.state.current_tick + int(result["elapsed_ticks"])
    assert after.state.resources[data.account_ids["buyer_currency"]].balance == Decimal("90")
    assert after.state.resources[data.account_ids["seller_currency"]].balance == Decimal("20")
    assert after.state.resources[data.account_ids["buyer_goods"]].balance == Decimal("2")
    assert after.state.resources[data.account_ids["stock"]].balance == Decimal("1")

    trade, = [event for event in committed_events if event.type == "core.market_trade_executed.v1"]
    typed_trade = parse_event({
        **trade.payload,
        "type": trade.type,
        "event_id": str(trade.event_id),
        "visibility": trade.visibility,
    })
    postings = ledger_postings(typed_trade)
    expected = sorted(
        (row.account_id.hex, row.resource_id, str(row.delta))
        for row in postings
    )
    with data.database.engine.connect() as connection:
        persisted = connection.exec_driver_sql(
            "SELECT account_id, resource_id, delta FROM ledger_postings WHERE event_id = ?",
            (trade.event_id.hex,),
        ).fetchall()
    assert sorted((row[0].replace("-", ""), row[1], row[2]) for row in persisted) == expected
    assert sum((Decimal(row[2]) for row in persisted if row[1] == CURRENCY_ID), Decimal("0")) == 0
    assert len(postings) == 4
    _assert_replay_matches_head(data)

    # An idempotent retry returns the original result and does not post payment twice.
    assert await data.service.send(data.identity, request) == result
    retried = _snapshot(data)
    assert retried.state_hash == after.state_hash
    assert retried.last_seq == after.last_seq
    assert retried.state.resources[data.account_ids["buyer_currency"]].balance == Decimal("90")
    assert len(data.models.calls) == 1
    _assert_replay_matches_head(data)


@pytest.mark.asyncio
async def test_model_cannot_invent_or_change_the_player_written_ceiling(market_conversation):
    data = market_conversation
    data.models.proposed_ceiling = {
        "max_total_minor": 10,
        "currency_resource_id": CURRENCY_ID,
    }
    before = _snapshot(data)
    request = _request(data, text="总价不超过9信用点就买两份矿石。")

    result = await data.service.send(data.identity, request)

    assert result["kind"] == "clarification"
    assert result["world_effect"] is False
    after = _snapshot(data)
    assert after.state_hash == before.state_hash
    assert after.state.current_tick == before.state.current_tick
    assert after.branch.state_version == before.branch.state_version
    assert after.last_seq == before.last_seq
    for account_id in data.account_ids.values():
        assert after.state.resources[account_id].balance == before.state.resources[account_id].balance
    assert len(data.repository.list_events(data.identity, data.world_id, data.branch_id).events) == 0
    _assert_replay_matches_head(data)


@pytest.mark.asyncio
async def test_model_cannot_add_a_ceiling_the_player_did_not_write(market_conversation):
    data = market_conversation
    data.models.proposed_ceiling = {
        "max_total_minor": 10,
        "currency_resource_id": CURRENCY_ID,
    }
    before = _snapshot(data)
    request = _request(data, text="买两份矿石。")

    result = await data.service.send(data.identity, request)

    assert result["kind"] == "clarification"
    assert result["world_effect"] is False
    after = _snapshot(data)
    assert after.state_hash == before.state_hash
    assert after.state.current_tick == before.state.current_tick
    assert after.branch.state_version == before.branch.state_version
    assert after.last_seq == before.last_seq
    assert len(data.repository.list_events(data.identity, data.world_id, data.branch_id).events) == 0
    _assert_replay_matches_head(data)


@pytest.mark.asyncio
async def test_unconditional_legacy_purchase_remains_available_without_a_ceiling(market_conversation):
    data = market_conversation
    before = _snapshot(data)
    request = _request(data, text="买两份矿石。")

    result = await data.service.send(data.identity, request)

    assert result["action_status"] == "succeeded"
    assert result["world_effect"] is True
    after = _snapshot(data)
    assert after.state.resources[data.account_ids["buyer_currency"]].balance == Decimal("90")
    assert after.state.resources[data.account_ids["buyer_goods"]].balance == Decimal("2")
    assert after.branch.state_version == before.branch.state_version + 1
    _assert_replay_matches_head(data)
