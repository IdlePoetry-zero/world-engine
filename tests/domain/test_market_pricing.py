from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.market_pricing import (
    PRICE_REVIEW_HANDLER_ID,
    PRICE_REVIEW_HANDLER_VERSION,
    PRICE_REVIEW_DAYS,
    PRICE_REVIEW_CONFIG_VERSION,
    MarketListingPricePolicy,
    MarketPricingConfig,
    MarketPriceInputError,
    MarketPriceUnavailable,
    StockPricePolicy,
    observe_market_offer,
    plan_market_price_review,
    plan_market_price_update,
    validate_market_pricing_config,
)
from world_engine.domain.market_pricing_reducers import (
    apply_market_price_review,
    validate_market_price_review_event,
)
from world_engine.domain.market_pricing_validation import validate_market_price_review_batch
from world_engine.domain.models import ResourceAccount, ScheduledEventRecord, WorldEntity, WorldState
from world_engine.domain.errors import InvalidEvent


def _market_state(
    quantities: list[tuple[str, int | None]],
    *,
    balance: str | None = None,
    tick: int = 10,
    inventory_revision: int = 2,
    market_revision: int = 3,
    offer_price: int = 40,
    market_id=None,
    seller_id=None,
    listing_id=None,
    stock_account_id=None,
):
    market_id = market_id or uuid4()
    seller_id = seller_id or uuid4()
    listing_id = listing_id or uuid4()
    stock_account_id = stock_account_id or uuid4()
    resource_id = "resource.wheat"
    lots = [
        {
            "lot_id": str(uuid4()),
            "account_id": str(stock_account_id),
            "resource_id": resource_id,
            "quantity": quantity,
            "expiry_tick": expiry_tick,
            "origin_event_id": str(uuid4()),
        }
        for quantity, expiry_tick in quantities
        if Decimal(quantity) > 0
    ]
    total = sum((Decimal(quantity) for quantity, _ in quantities), Decimal("0"))
    account_balance = Decimal(balance) if balance is not None else total
    state = WorldState(
        current_tick=tick,
        entities={
            market_id: WorldEntity(
                id=market_id,
                kind="place",
                name="Market",
                components={
                    "place": {"place_type": "settlement"},
                    "market": {
                        "revision": market_revision,
                        "currency_resource_id": "core.coin",
                        "offers": [
                            {
                                "listing_id": str(listing_id),
                                "resource_id": resource_id,
                                "stock_account_id": str(stock_account_id),
                                "seller_id": str(seller_id),
                                "unit_price_minor": offer_price,
                                "minimum_quantity": "1",
                                "active": True,
                            }
                        ],
                    },
                },
            ),
            seller_id: WorldEntity(
                id=seller_id,
                kind="character",
                name="Seller",
                location_id=market_id,
                components={"inventory": {"revision": inventory_revision, "lots": lots}},
            ),
        },
        resources={
            stock_account_id: ResourceAccount(
                resource_id=resource_id,
                balance=account_balance,
                owner_id=seller_id,
            )
        },
    )
    return state, market_id, listing_id


def _policy(**updates):
    fields = {
        "policy_id": "market.wheat",
        "revision": 1,
        "reference_price_minor": 100,
        "target_stock_quantity": "10",
        "sensitivity_bps": 10_000,
        "max_markup_bps": 5_000,
        "max_discount_bps": 5_000,
    }
    fields.update(updates)
    return StockPricePolicy(**fields)


def _configured_review_state(
    quantities: list[tuple[str, int | None]],
    *,
    current_price: int = 40,
    seller_status: str = "active",
    listing_active: bool = True,
    completed_runs: int = 0,
    max_runs: int = 2,
    schedule_status: str | None = None,
    market_id=None,
    seller_id=None,
    listing_id=None,
    stock_account_id=None,
    schedule_id=None,
):
    state, market_id, listing_id = _market_state(
        quantities,
        tick=PRICE_REVIEW_DAYS * 86_400,
        offer_price=current_price,
        market_id=market_id,
        seller_id=seller_id,
        listing_id=listing_id,
        stock_account_id=stock_account_id,
    )
    market_entity = state.entities[market_id]
    market_json = market_entity.components["market"]
    offer = market_json["offers"][0]
    offer["active"] = listing_active
    state.entities[market_id] = market_entity.model_copy(
        update={"components": dict(market_entity.components) | {"market": market_json}}
    )
    seller_id = next(entity.id for entity in state.entities.values() if entity.kind == "character")
    seller = state.entities[seller_id]
    state.entities[seller_id] = seller.model_copy(update={"status": seller_status})
    schedule_id = schedule_id or uuid4()
    schedule = ScheduledEventRecord(
        id=schedule_id,
        due_tick=state.current_tick,
        phase="maintenance",
        priority=0,
        handler_id=PRICE_REVIEW_HANDLER_ID,
        handler_version=PRICE_REVIEW_HANDLER_VERSION,
        repeat_ticks=PRICE_REVIEW_DAYS * state.calendar.day_seconds,
        max_runs=max_runs,
        completed_runs=completed_runs,
        status=schedule_status or ("completed" if completed_runs == max_runs else "queued"),
    )
    policy = MarketListingPricePolicy(
        **_policy().model_dump(mode="python"),
        market_id=market_id,
        listing_id=listing_id,
    )
    config = MarketPricingConfig(
        schema_version=PRICE_REVIEW_CONFIG_VERSION,
        review_period_days=PRICE_REVIEW_DAYS,
        policies=[policy],
    )
    state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {
            "scheduled_events": [schedule],
            "metadata": {"market_pricing": config.model_dump(mode="json")},
        }
    )
    return state, market_id, listing_id, schedule


def _plan(state, market_id, listing_id, *, policy=None, schedule_id=None, period=1):
    observation = observe_market_offer(state, market_id, listing_id, at_tick=state.current_tick)
    return plan_market_price_update(
        observation,
        policy or _policy(),
        schedule_id=schedule_id or uuid4(),
        period_index=period,
        due_tick=state.current_tick,
    )


def _review_event(state, schedule, *, world_id=None, branch_id=None, command_id=None):
    return plan_market_price_review(
        state,
        schedule,
        world_id=world_id or uuid4(),
        branch_id=branch_id or uuid4(),
        command_id=command_id or uuid4(),
        ruleset_version="1.0.0",
    )


def test_observation_counts_only_unexpired_fifo_lots_and_is_pure():
    state, market_id, listing_id = _market_state(
        [("2", 11), ("3", 10), ("4", None), ("1", 20)]
    )
    before = state.model_dump(mode="json")

    observation = observe_market_offer(state, market_id, listing_id, at_tick=10)

    assert observation.tradable_quantity == Decimal("7")
    assert observation.stock_account_balance == Decimal("10")
    assert observation.eligible_lot_count == 3
    assert observation.market_revision == 3
    assert observation.seller_inventory_revision == 2
    assert observation.current_unit_price_minor == 40
    assert len(observation.snapshot_digest) == 64
    assert state.model_dump(mode="json") == before


def test_stock_curve_is_monotonic_and_keeps_integer_price_nonzero():
    schedule_id = uuid4()
    outputs = []
    for amount in ("0", "5", "10", "20"):
        state, market_id, listing_id = _market_state([(amount, None)] if amount != "0" else [])
        outputs.append(
            _plan(state, market_id, listing_id, schedule_id=schedule_id, policy=_policy())
        )

    assert [item.unit_price_minor for item in outputs] == [150, 150, 100, 50]
    assert all(isinstance(item.unit_price_minor, int) and item.unit_price_minor >= 1 for item in outputs)
    assert outputs[0].pressure_bps == 5_000
    assert outputs[2].pressure_bps == 0
    assert outputs[3].pressure_bps == -5_000


@pytest.mark.parametrize(
    ("quantity", "floor", "ceiling", "expected"),
    [
        ("0", 1, 120, 120),
        ("100", 60, 1_000, 60),
        ("10", 1, 1_000, 100),
    ],
)
def test_price_respects_floor_cap_and_reference(quantity, floor, ceiling, expected):
    state, market_id, listing_id = _market_state([(quantity, None)] if quantity != "0" else [])
    policy = _policy(price_floor_minor=floor, price_ceiling_minor=ceiling)

    proposal = _plan(state, market_id, listing_id, policy=policy)

    assert proposal.unit_price_minor == expected
    assert proposal.unit_price_minor >= 1


def test_schedule_retry_has_stable_id_and_snapshot_changes_invalidate_cas():
    schedule_id = uuid4()
    state_a, market_id, listing_id = _market_state([("4", 100)], balance="4")
    proposal_a = _plan(state_a, market_id, listing_id, schedule_id=schedule_id)
    retry = _plan(state_a, market_id, listing_id, schedule_id=schedule_id)

    seller_id = next(entity.id for entity in state_a.entities.values() if entity.kind == "character")
    stock_account_id = next(iter(state_a.resources))
    state_b, _, _ = _market_state(
        [("5", 100)],
        balance="5",
        market_id=market_id,
        seller_id=seller_id,
        listing_id=listing_id,
        stock_account_id=stock_account_id,
    )
    proposal_b = _plan(state_b, market_id, listing_id, schedule_id=schedule_id)

    assert retry.assessment_id == proposal_a.assessment_id
    assert proposal_b.assessment_id == proposal_a.assessment_id
    assert retry.snapshot_digest == proposal_a.snapshot_digest
    assert proposal_b.snapshot_digest != proposal_a.snapshot_digest
    assert proposal_b.expected_stock_account_balance == Decimal("5")
    assert proposal_b.observed_stock_quantity == Decimal("5")


def test_plan_rejects_non_due_observation_and_invalid_listing_state():
    state, market_id, listing_id = _market_state([("2", None)])
    observation = observe_market_offer(state, market_id, listing_id, at_tick=10)

    with pytest.raises(MarketPriceInputError, match="exact due tick"):
        plan_market_price_update(
            observation,
            _policy(),
            schedule_id=uuid4(),
            period_index=1,
            due_tick=11,
        )
    with pytest.raises(MarketPriceInputError, match="current tick"):
        observe_market_offer(state, market_id, listing_id, at_tick=11)


@pytest.mark.parametrize(
    "fields",
    [
        {"target_stock_quantity": "0"},
        {"target_stock_quantity": 0.1},
        {"reference_price_minor": 0},
        {"price_floor_minor": 0},
        {"max_discount_bps": 10_000},
        {"price_floor_minor": 100, "price_ceiling_minor": 99},
    ],
)
def test_policy_rejects_zero_float_or_unbounded_price_inputs(fields):
    with pytest.raises(ValidationError):
        _policy(**fields)


def test_observation_rejects_wrong_stock_owner():
    state, market_id, listing_id = _market_state([("1", None)])
    account_id = next(iter(state.resources))
    account = state.resources[account_id]
    wrong_owner = uuid4()
    state.resources[account_id] = ResourceAccount(
        resource_id=account.resource_id,
        balance=account.balance,
        owner_id=wrong_owner,
    )
    # WorldState cross-validation normally catches this malformed projection.
    # Use an unsafe copy solely to assert that the observer has its own guard.
    malformed = state.model_copy(update={"resources": state.resources})

    with pytest.raises(MarketPriceInputError, match="stock account"):
        observe_market_offer(malformed, market_id, listing_id, at_tick=10)


def test_market_price_review_changes_offer_and_recurrence_in_one_typed_event():
    state, market_id, listing_id, schedule = _configured_review_state([("4", None)])

    config = validate_market_pricing_config(state)
    event = _review_event(state, schedule)

    assert config is not None
    assert event.assessment_count == 1
    assert event.changed_count == 1
    assert event.unchanged_count == 0
    assert event.skipped_count == 0
    assert event.changes[0].listing_id == listing_id
    assert event.changes[0].previous_unit_price_minor == 40
    assert event.changes[0].new_unit_price_minor == 150
    validate_market_price_review_event(state, event)

    changed = apply_market_price_review(state, event)
    market = changed.entities[market_id].components["market"]
    new_schedule = changed.scheduled_events[0]

    assert market["revision"] == 4
    assert market["offers"][0]["unit_price_minor"] == 150
    assert new_schedule.status == "queued"
    assert new_schedule.completed_runs == 1
    assert new_schedule.due_tick == state.current_tick + 30 * state.calendar.day_seconds
    assert new_schedule.revision == schedule.revision + 1
    assert changed.resources == state.resources
    assert changed.entities[next(item for item in changed.entities if item != market_id)].components == state.entities[
        next(item for item in state.entities if item != market_id)
    ].components


def test_market_price_review_is_idempotent_and_stale_inventory_cas_is_rejected():
    state, market_id, listing_id, schedule = _configured_review_state([("4", None)])
    context = {"world_id": uuid4(), "branch_id": uuid4(), "command_id": uuid4()}
    event = _review_event(state, schedule, **context)
    retry = _review_event(state, schedule, **context)
    assert retry.model_dump(mode="json") == event.model_dump(mode="json")

    seller_id = event.changes[0].seller_id
    stock_account_id = event.changes[0].stock_account_id
    state_changed, _, _, _ = _configured_review_state(
        [("5", None)],
        market_id=market_id,
        seller_id=seller_id,
        listing_id=listing_id,
        stock_account_id=stock_account_id,
        schedule_id=schedule.id,
    )
    with pytest.raises(InvalidEvent, match="canonical stock snapshot"):
        apply_market_price_review(state_changed, event)


@pytest.mark.parametrize(
    ("seller_status", "listing_active", "expected_reason"),
    [
        ("dead", True, "seller_inactive"),
        ("active", False, "listing_inactive"),
    ],
)
def test_unavailable_seller_or_inactive_offer_is_skipped_without_changes(
    seller_status, listing_active, expected_reason
):
    state, market_id, _listing_id, schedule = _configured_review_state(
        [("0", None)],
        seller_status=seller_status,
        listing_active=listing_active,
    )

    event = _review_event(state, schedule)
    changed = apply_market_price_review(state, event)

    assert event.changed_count == 0
    assert event.skipped_count == 1
    assert event.unchanged_count == 0
    assert event.changes == []
    assert changed.entities[market_id].components["market"] == state.entities[market_id].components["market"]
    assert changed.resources == state.resources
    assert changed.scheduled_events[0].completed_runs == 1
    with pytest.raises(MarketPriceUnavailable) as unavailable:
        observe_market_offer(state, market_id, _listing_id, at_tick=state.current_tick)
    assert unavailable.value.code == expected_reason


def test_zero_lot_stock_is_a_bounded_quote_and_does_not_create_stock():
    state, market_id, _listing_id, schedule = _configured_review_state([])
    before_entity_ids = set(state.entities)
    before_resource_ids = set(state.resources)

    event = _review_event(state, schedule)
    changed = apply_market_price_review(state, event)

    assert event.changed_count == 1
    assert event.changes[0].new_unit_price_minor == 150
    assert set(changed.entities) == before_entity_ids
    assert set(changed.resources) == before_resource_ids
    assert changed.entities[market_id].components["market"]["offers"][0]["unit_price_minor"] == 150


def test_market_pricing_config_is_required_to_use_listed_market_offer():
    state, market_id, listing_id, _schedule = _configured_review_state([("1", None)])
    config = MarketPricingConfig.model_validate(state.metadata["market_pricing"])
    invalid = MarketPricingConfig(
        schema_version=PRICE_REVIEW_CONFIG_VERSION,
        review_period_days=PRICE_REVIEW_DAYS,
        policies=[
            MarketListingPricePolicy(
                **_policy().model_dump(mode="python"),
                market_id=market_id,
                listing_id=uuid4(),
            )
        ]
    )
    malformed = state.model_copy(update={"metadata": {"market_pricing": invalid.model_dump(mode="json")}})

    assert config.policies[0].listing_id == listing_id
    with pytest.raises(MarketPriceInputError, match="does not exist"):
        validate_market_pricing_config(malformed)


def test_terminal_price_schedule_is_valid_but_config_without_schedule_is_not():
    terminal, _market_id, _listing_id, _schedule = _configured_review_state(
        [("1", None)], completed_runs=1, max_runs=1
    )
    config = validate_market_pricing_config(terminal)
    assert config is not None

    no_schedule = terminal.model_copy(update={"scheduled_events": []})
    with pytest.raises(MarketPriceInputError, match="exactly one"):
        validate_market_pricing_config(no_schedule)

    no_config = terminal.model_copy(update={"metadata": {}})
    with pytest.raises(MarketPriceInputError, match="without a versioned world config"):
        validate_market_pricing_config(no_config)


def test_host_batch_binds_review_to_one_persisted_simulation_window():
    state, _market_id, _listing_id, schedule = _configured_review_state([("4", None)])
    context = {"world_id": uuid4(), "branch_id": uuid4(), "command_id": uuid4()}
    event = _review_event(state, schedule, **context)
    payload = {"job_id": str(uuid4()), "next_tick": str(state.current_tick)}

    validate_market_price_review_batch(
        [event],
        **context,
        ruleset_version="1.0.0",
        command_type="simulation.window",
        command_payload=payload,
    )
    validate_market_price_review_batch(
        [event],
        **context,
        ruleset_version="1.0.0",
        command_type=None,
        command_payload=None,
    )

    with pytest.raises(InvalidEvent, match="simulation window"):
        validate_market_price_review_batch(
            [event],
            **context,
            ruleset_version="1.0.0",
            command_type="action.trade",
            command_payload=payload,
        )
    with pytest.raises(InvalidEvent, match="exact due tick"):
        validate_market_price_review_batch(
            [event],
            **context,
            ruleset_version="1.0.0",
            command_type="simulation.window",
            command_payload=payload | {"next_tick": str(state.current_tick + 1)},
        )
    with pytest.raises(InvalidEvent, match="standalone"):
        validate_market_price_review_batch(
            [event, object()],
            **context,
            ruleset_version="1.0.0",
            command_type="simulation.window",
            command_payload=payload,
        )
