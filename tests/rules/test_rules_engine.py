from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.application.visibility import project_player_state
from world_engine.domain.components import PlaceRoute, ProductionQuotaPolicy, ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import EntityDiscoveredEvent, LifeEndedEvent, MarketTradeExecutedEvent
from world_engine.domain.models import EntityFieldChangedEvent, ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine, parse_action, route_accessible


def _ids(count: int) -> list:
    return [uuid4() for _ in range(count)]


def _context(actor_id, command_id=None):
    return RuleContext(
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=command_id or uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _base_state(*, actor_components=None, place_components=None, extra_entities=None, resources=None, tick=0, place_actor_ids=None):
    place_id, actor_id = place_actor_ids or _ids(2)
    entities = {
        place_id: WorldEntity(
            id=place_id,
            kind="place",
            name="Market Square",
            components={"place": {"place_type": "settlement", "routes": []}, **(place_components or {})},
        ),
        actor_id: WorldEntity(
            id=actor_id,
            kind="character",
            name="Player",
            location_id=place_id,
            components={
                "health": {"current": 80, "maximum": 100},
                "needs": {
                    "food": {"value": 60, "critical_below": 20},
                    "water": {"value": 70, "critical_below": 20},
                    "rest": {"value": 40},
                },
                "capabilities": {"grants": ["world.travel", "world.work"], "restrictions": []},
                "skills": {"skills": {}},
                "progression": {},
                "inventory": {"lots": []},
                **(actor_components or {}),
            },
        ),
    }
    if extra_entities:
        entities.update(extra_entities)
    state = WorldState(
        current_tick=tick,
        entities=entities,
        resources=resources or {},
        player_id=actor_id,
    )
    return state, place_id, actor_id


def _replay(state: WorldState, events):
    result = state
    for event in events:
        result = apply_event(result, event)
    return result


def test_discovery_unlocks_the_hidden_route_only_for_the_discoverer():
    engine = RuleEngine()
    square, player, witness, vault, route_id = _ids(5)
    route = PlaceRoute(route_id=route_id, from_place_id=square, to_place_id=vault, travel_ticks=60)
    state, _, _ = _base_state(
        place_actor_ids=(square, player),
        place_components={"place": {"routes": [route]}},
        extra_entities={
            witness: WorldEntity(id=witness, kind="character", location_id=square,
                                 components={"capabilities": {"grants": ["world.travel"]}}),
            vault: WorldEntity(id=vault, kind="place", name="Archive Vault", location_id=square,
                               components={"hidden": True}),
        },
    )
    assert not route_accessible(state, state.entities[player], route)
    proposal = {"kind": "explore", "place_id": square}
    context = RuleContext(world_id=UUID(int=1), branch_id=UUID(int=2), command_id=UUID(int=3),
                          actor_id=player, ruleset_version="1.0.0")
    result = engine.evaluate(state, proposal, context)
    discovery = next(event for event in result.events if isinstance(event, EntityDiscoveredEvent))
    # An interval host has already advanced the shared clock when it resolves a search.
    search_state = apply_event(state, next(event for event in result.events if isinstance(event, TimeAdvancedEvent)))
    search_intent = parse_action(proposal)
    boundary = engine.settle_exploration(search_state, search_intent, context)
    assert boundary.outcome == result.outcome and boundary.rolls == result.rolls
    search_event_types = {"core.entity_discovered.v1", "core.knowledge_acquired.v1"}
    assert boundary.events == [event for event in result.events if event.type in search_event_types]
    search_after = _replay(search_state, boundary.events)
    assert search_after.current_tick == search_state.current_tick
    assert search_after.entities[player].components.get("needs") == search_state.entities[player].components.get("needs")
    changed_site = apply_event(search_state, EntityFieldChangedEvent(entity_id=vault, field_name="status", value="retired"))
    changed_boundary = engine.settle_exploration(changed_site, search_intent, context)
    assert changed_boundary.outcome.code == "nothing_hidden_found" and not changed_boundary.events

    after = _replay(state, result.events)
    assert after.entities[vault].components["hidden"] is True
    projection = after.model_dump(mode="json")
    assert str(vault) in project_player_state(projection, str(player))["entities"]
    assert str(vault) not in project_player_state(projection, str(witness))["entities"]
    assert route_accessible(after, after.entities[player], route)
    assert not route_accessible(after, after.entities[witness], route)
    observation = next(record for record in after.knowledge if record.owner_id == player and record.subject_id == vault)
    assert observation.source_event_id == discovery.event_id
    assert observation.fact is not None
    assert observation.fact.predicate == "entity_location"
    assert observation.fact.value == square
    assert observation.fact.effective_tick == after.current_tick
    move = engine.evaluate(after, {"kind": "move", "destination_id": vault}, _context(player))
    assert move.outcome.status == "succeeded"
    assert _replay(after, move.events).entities[player].location_id == vault
    repeat = engine.evaluate(after, proposal, _context(player))
    assert repeat.outcome.code == "nothing_hidden_found"


def test_history_exploration_focus_matches_authored_site_places():
    site = WorldEntity(
        id=uuid4(),
        kind="place",
        name="Old Foundation",
        components={"place": {"place_type": "site"}, "hidden": True},
    )
    settlement = WorldEntity(
        id=uuid4(),
        kind="place",
        name="Market Square",
        components={"place": {"place_type": "settlement"}, "hidden": True},
    )

    assert RuleEngine._focus_matches(site, "history")
    assert not RuleEngine._focus_matches(settlement, "history")


def test_action_intents_are_closed_and_wait_alias_is_typed():
    parsed = parse_action({"kind": "idle", "duration_ticks": 60})
    assert parsed.kind == "idle"
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({"kind": "wait", "duration_ticks": 60, "actor_id": str(uuid4())})
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({"kind": "freeform", "patch": {"status": "dead"}})


def test_rest_and_wait_use_world_seconds_and_carry_partial_daily_need_rates():
    state, _place_id, actor_id = _base_state()
    engine = RuleEngine()
    first = engine.evaluate(state, {"kind": "wait", "duration_ticks": 3600}, _context(actor_id))
    after_first = _replay(state, first.events)
    needs_first = after_first.entities[actor_id].components["needs"]
    assert first.events[0].type == "core.time_advanced.v1"
    assert first.elapsed_ticks == 3600
    assert needs_first["food"]["value"] == 60
    assert needs_first["food"]["decay_remainder"] == 64_800

    second = engine.evaluate(after_first, {"kind": "wait", "duration_ticks": 3600}, _context(actor_id))
    after_second = _replay(after_first, second.events)
    needs_second = after_second.entities[actor_id].components["needs"]
    assert needs_second["food"]["value"] == 59
    assert needs_second["food"]["decay_remainder"] == 43_200

    resting = engine.evaluate(
        state,
        {"kind": "rest", "duration_ticks": 86_400},
        _context(actor_id),
    )
    after_rest = _replay(state, resting.events)
    needs_rest = after_rest.entities[actor_id].components["needs"]
    assert needs_rest["food"]["value"] == 42
    assert needs_rest["water"]["value"] == 40
    assert needs_rest["rest"]["value"] == 76
    assert after_rest.entities[actor_id].components["health"]["current"] > 80


def test_wait_stops_before_queued_schedule_boundary_and_resolves_lifespan_at_exact_end():
    state, _place_id, actor_id = _base_state(
        actor_components={"life": {"birth_tick": 0, "lifespan_ticks": 10_000}}
    )
    state_payload = state.model_dump(mode="json")
    state_payload["scheduled_events"] = [
        {
            "id": str(uuid4()),
            "due_tick": 900,
            "phase": "maintenance",
            "handler_id": "core.test",
            "handler_version": "1.0.0",
        }
    ]
    state = WorldState.model_validate(state_payload)
    blocked = RuleEngine().evaluate(state, {"kind": "wait", "duration_ticks": 1000}, _context(actor_id))
    assert blocked.outcome.code == "scheduled_boundary_required"
    assert blocked.events == [] and blocked.elapsed_ticks == 0

    state_payload = state.model_dump(mode="json")
    state_payload["scheduled_events"] = []
    state = WorldState.model_validate(state_payload)
    expiry_state = WorldState.model_validate(
        {
            **state.model_dump(mode="json"),
            "entities": {
                str(key): value.model_dump(mode="json")
                | ({"components": {**value.components, "life": {"birth_tick": 0, "lifespan_ticks": 3600}}}
                   if key == actor_id else {})
                for key, value in state.entities.items()
            },
        }
    )
    expired = RuleEngine().evaluate(expiry_state, {"kind": "wait", "duration_ticks": 3600}, _context(actor_id))
    after = _replay(expiry_state, expired.events)
    assert expired.outcome.code == "lifespan_expired"
    assert after.entities[actor_id].status == "dead"
    assert after.entities[actor_id].components["health"]["current"] == 0
    with pytest.raises(InvalidEvent, match="before its registered lifespan"):
        apply_event(
            expiry_state,
            LifeEndedEvent(entity_id=actor_id, expected_life_revision=1),
        )


def test_simultaneous_cohort_lifespan_deaths_use_sequential_population_revisions():
    state, place_id, actor_id = _base_state(
        actor_components={"life": {"birth_tick": 0, "lifespan_ticks": 3600}}
    )
    cohort_id, second_id = _ids(2)
    cohort = WorldEntity(
        id=cohort_id,
        kind="group",
        name="Harbor residents",
        location_id=place_id,
        components={"population": {"species_id": "core.human", "headcount": 2, "unmodeled_headcount": 0}},
    )
    actor = state.entities[actor_id]
    actor_life = {**actor.components["life"], "population_group_id": str(cohort_id)}
    actor = actor.model_copy(update={"components": {**actor.components, "life": actor_life}})
    second = WorldEntity(
        id=second_id,
        kind="character",
        location_id=place_id,
        components={
            "life": {"birth_tick": 0, "lifespan_ticks": 3600, "population_group_id": str(cohort_id)},
            "health": {"current": 100, "maximum": 100},
        },
    )
    state = WorldState.model_validate(
        {
            **state.model_dump(mode="json"),
            "entities": {**state.entities, actor_id: actor, cohort_id: cohort, second_id: second},
        }
    )

    resolution = RuleEngine().evaluate(state, {"kind": "wait", "duration_ticks": 3600}, _context(actor_id))
    deaths = [event for event in resolution.events if isinstance(event, LifeEndedEvent)]
    ended = _replay(state, resolution.events)

    assert len(deaths) == 2
    assert [event.expected_population_revision for event in deaths] == [1, 2]
    assert ended.entities[cohort_id].components["population"]["headcount"] == 0
    assert ended.entities[cohort_id].components["population"]["revision"] == 3


def test_route_predicate_and_movement_share_access_policy_and_second_duration():
    place_id, actor_id, dest_id, route_id = _ids(4)
    route = PlaceRoute(
        route_id=route_id,
        from_place_id=place_id,
        to_place_id=dest_id,
        travel_ticks=7200,
        travel_modes=["travel.walk"],
    )
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", components={"place": {"routes": [route.model_dump(mode="json")]}}),
        dest_id: WorldEntity(id=dest_id, kind="place", name="North Gate", components={"place": {"access_policy": "open"}}),
        actor_id: WorldEntity(
            id=actor_id,
            kind="character",
            location_id=place_id,
            components={"capabilities": {"grants": ["world.travel"], "restrictions": []}},
        ),
    }
    state = WorldState(entities=entities, player_id=actor_id)
    actor = state.entities[actor_id]
    assert route_accessible(state, actor, route)
    result = RuleEngine().evaluate(
        state,
        {"kind": "move", "destination_id": str(dest_id), "route_id": str(route_id)},
        _context(actor_id),
    )
    after = _replay(state, result.events)
    assert result.elapsed_ticks == 7200
    assert result.events[0].type == "core.time_advanced.v1"
    assert after.entities[actor_id].location_id == dest_id

    denied = actor.model_copy(
        update={"components": {"capabilities": {"grants": [], "restrictions": []}}}
    )
    assert not route_accessible(state, denied, route)


def test_malformed_action_returns_bounded_rejection_without_state_effects():
    state, _place_id, actor_id = _base_state()
    before = state.model_dump(mode="json")
    result = RuleEngine().evaluate(
        state,
        {"kind": "wait", "duration_ticks": 0, "untrusted_extra": "x" * 20_000},
        _context(actor_id),
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "invalid_action"
    assert result.outcome.message == "The action is malformed or does not match a registered action."
    assert len(result.outcome.message) <= 1000
    assert "untrusted_extra" not in result.outcome.message
    assert result.events == []
    assert result.elapsed_ticks == 0
    assert state.model_dump(mode="json") == before


@pytest.mark.parametrize(("need", "resource", "restored"), [
    ("food", "core.food", 70),
    ("water", "core.water", 80),
    ("water", "cultivation.spirit_water", 80),
])
def test_consume_decrements_fifo_inventory_and_balances_consumption_ledger(need, resource, restored):
    place_id, actor_id = _ids(2)
    food_account, sink_account, lot_id = _ids(3)
    resources = {
        food_account: ResourceAccount(resource_id=resource, balance="3", owner_id=actor_id),
        sink_account: ResourceAccount(resource_id=resource, balance="0", account_type="consumption_sink"),
    }
    state, place_id, actor_id = _base_state(
        actor_components={"inventory": {"lots": [
            {"lot_id": lot_id, "account_id": food_account, "resource_id": resource, "quantity": "3"}
        ]}},
        resources=resources,
        place_actor_ids=(place_id, actor_id),
    )
    if resource == "cultivation.spirit_water":
        state = state.model_copy(update={"metadata": {"resource_roles": {"water_resource_id": resource}}})
    result = RuleEngine().evaluate(
        state,
        {"kind": "consume", "need": need, "resource_id": resource, "quantity": "1"},
        _context(actor_id),
    )
    after = _replay(state, result.events)
    assert result.outcome.status == "succeeded"
    assert after.resources[food_account].balance == Decimal("2")
    assert after.resources[sink_account].balance == Decimal("1")
    assert after.entities[actor_id].components["inventory"]["lots"][0]["quantity"] == "2"
    assert after.entities[actor_id].components["needs"][need]["value"] == restored


def test_trade_atomically_updates_inventory_lots_and_both_resource_ledgers():
    place_id, buyer_id, seller_id, listing_id, stock_id, seller_lot_id, buyer_money_id, seller_money_id, buyer_cash_lot, seller_cash_lot = _ids(10)
    buyer_goods_id = uuid4()
    offer = {
        "listing_id": str(listing_id),
        "resource_id": "core.ore",
        "stock_account_id": str(stock_id),
        "seller_id": str(seller_id),
        "unit_price_minor": 3,
        "minimum_quantity": "1",
        "active": True,
    }
    place = WorldEntity(
        id=place_id,
        kind="place",
        components={"place": {"place_type": "settlement"}, "market": {"currency_resource_id": "core.coin", "offers": [offer]}},
    )
    buyer = WorldEntity(
        id=buyer_id, kind="character", location_id=place_id,
        components={"inventory": {"lots": [
            {"lot_id": buyer_cash_lot, "account_id": buyer_money_id, "resource_id": "core.coin", "quantity": "100"}
        ]}},
    )
    seller = WorldEntity(
        id=seller_id, kind="character", location_id=place_id,
        components={"inventory": {"lots": [
            {"lot_id": seller_lot_id, "account_id": stock_id, "resource_id": "core.ore", "quantity": "5"},
            {"lot_id": seller_cash_lot, "account_id": seller_money_id, "resource_id": "core.coin", "quantity": "10"},
        ]}},
    )
    state = WorldState(
        entities={place_id: place, buyer_id: buyer, seller_id: seller},
        resources={
            stock_id: ResourceAccount(resource_id="core.ore", balance="5", owner_id=seller_id),
            buyer_money_id: ResourceAccount(resource_id="core.coin", balance="100", owner_id=buyer_id),
            seller_money_id: ResourceAccount(resource_id="core.coin", balance="10", owner_id=seller_id),
        },
        player_id=buyer_id,
    )
    result = RuleEngine().evaluate(
        state,
        {"kind": "trade", "listing_id": str(listing_id), "quantity": "2"},
        _context(buyer_id),
    )
    after = _replay(state, result.events)
    goods_id = next(
        account_id for account_id, account in after.resources.items()
        if account.resource_id == "core.ore" and account.owner_id == buyer_id
    )
    assert after.resources[stock_id].balance == Decimal("3")
    assert after.resources[goods_id].balance == Decimal("2")
    assert after.resources[buyer_money_id].balance == Decimal("94")
    assert after.resources[seller_money_id].balance == Decimal("16")
    buyer_currency_total = sum(
        Decimal(lot["quantity"]) for lot in after.entities[buyer_id].components["inventory"]["lots"]
        if lot["account_id"] == str(buyer_money_id)
    )
    seller_currency_total = sum(
        Decimal(lot["quantity"]) for lot in after.entities[seller_id].components["inventory"]["lots"]
        if lot["account_id"] == str(seller_money_id)
    )
    seller_goods_total = sum(
        Decimal(lot["quantity"]) for lot in after.entities[seller_id].components["inventory"]["lots"]
        if lot["account_id"] == str(stock_id)
    )
    assert buyer_currency_total == 94
    assert seller_currency_total == 16
    assert seller_goods_total == 3
    assert sum(
        Decimal(lot["quantity"]) for lot in after.entities[buyer_id].components["inventory"]["lots"]
        if lot["account_id"] == str(goods_id)
    ) == 2
    trade = next(event for event in result.events if isinstance(event, MarketTradeExecutedEvent))
    postings = [event for event in result.events if isinstance(event, MarketTradeExecutedEvent)]
    assert len(postings) == 1 and trade.unit_price_minor == 3


def test_production_consumes_registered_recipe_inputs_and_finite_output_quota():
    place_id, actor_id, input_account_id, output_account_id, source_id, sink_id, input_lot_id = _ids(7)
    production = {
        "recipes": [
            {
                "recipe_id": "recipe.core.tools",
                "inputs": [{"resource_id": "core.wood", "quantity": "2"}],
                "outputs": [{"resource_id": "core.tool", "quantity": "1"}],
                "duration_ticks": 3600,
                "required_skill": "skill.local_knowledge",
                "required_skill_level": "10",
            }
        ]
    }
    state, _place_id, actor_id = _base_state(
        place_components={
            "production": production,
            "inventory": {"lots": [{"lot_id": input_lot_id, "account_id": input_account_id,
                                     "resource_id": "core.wood", "quantity": "5"}]},
        },
        actor_components={"skills": {"skills": {"skill.local_knowledge": {"level": "10"}}}},
        place_actor_ids=(place_id, actor_id),
        resources={
            input_account_id: ResourceAccount(resource_id="core.wood", balance="5", owner_id=place_id),
            output_account_id: ResourceAccount(resource_id="core.tool", balance="0", owner_id=place_id),
            source_id: ResourceAccount(resource_id="core.tool", balance="0", owner_id=place_id,
                                       account_type="production_source", credit_limit="10"),
            sink_id: ResourceAccount(resource_id="core.wood", balance="0", account_type="consumption_sink"),
        },
    )
    result = RuleEngine().evaluate(
        state,
        {"kind": "produce", "producer_id": str(place_id), "recipe_id": "recipe.core.tools", "batch_count": 2},
        _context(actor_id),
    )
    after = _replay(state, result.events)
    assert after.current_tick == 7200
    assert after.resources[input_account_id].balance == Decimal("1")
    assert after.resources[output_account_id].balance == Decimal("2")
    assert after.resources[source_id].balance == Decimal("-2")
    assert after.resources[sink_id].balance == Decimal("4")
    assert sum(Decimal(lot["quantity"]) for lot in after.entities[place_id].components["inventory"]["lots"]) == 3


def test_training_is_repeatable_and_combat_roll_is_seeded_by_command():
    state, place_id, actor_id = _base_state(
        actor_components={"skills": {"skills": {"skill.combat": {"level": "20"}}}}
    )
    target_id = uuid4()
    target = WorldEntity(
        id=target_id,
        kind="character",
        location_id=place_id,
        components={"health": {"current": 50, "maximum": 50}},
    )
    entities = {**state.entities, target_id: target}
    state = WorldState.model_validate({**state.model_dump(mode="json"), "entities": entities})
    context = _context(actor_id)
    first = RuleEngine().evaluate(state, {"kind": "attack", "target_id": str(target_id)}, context)
    second = RuleEngine().evaluate(state, {"kind": "attack", "target_id": str(target_id)}, context)
    assert first.rolls[0].digest == second.rolls[0].digest
    assert first.rolls[0].value == second.rolls[0].value
    after_attack = _replay(state, first.events)
    if first.rolls[0].outcome == "hit":
        assert after_attack.entities[target_id].components["health"]["current"] < 50
    else:
        assert after_attack.entities[target_id].components["health"]["current"] == 50

    training = RuleEngine().evaluate(
        state,
        {"kind": "train", "skill_id": "skill.combat", "duration_ticks": 3600},
        _context(actor_id),
    )
    unregistered = RuleEngine().evaluate(
        state, {"kind": "train", "skill_id": "skill.unavailable", "duration_ticks": 3600}, context,
    )
    assert unregistered.outcome.code == "skill_not_registered"
    assert unregistered.events == [] and unregistered.elapsed_ticks == 0
    trained = _replay(state, training.events)
    skills = trained.entities[actor_id].components["skills"]["skills"]["skill.combat"]
    assert training.elapsed_ticks == 3600
    assert skills["experience"] == 12
    assert Decimal(skills["level"]) == Decimal("20.12")


def test_scheduled_quota_renewal_emits_finite_quota_and_completion_events():
    place_id, actor_id = _ids(2)
    source_id, schedule_id = _ids(2)
    quota = ProductionQuotaPolicy(
        quota_id="seasonal_yield",
        resource_id="core.grain",
        source_account_id=source_id,
        period_ticks=10,
        per_period="2",
        maximum_renewal="3",
        base_credit_limit="1",
    )
    schedule = ScheduledEventRecord(
        id=schedule_id,
        due_tick=10,
        phase="environment",
        handler_id="production.quota_renewal",
        handler_version="1.0.0",
        repeat_ticks=10,
        max_runs=3,
        payload={"producer_id": str(place_id), "quota_id": "seasonal_yield"},
    )
    state, _, actor_id = _base_state(
        place_actor_ids=(place_id, actor_id),
        place_components={"production": {"quotas": [quota.model_dump(mode="json")]}},
        resources={
            source_id: ResourceAccount(
                resource_id="core.grain",
                balance="0",
                owner_id=place_id,
                account_type="production_source",
                credit_limit="1",
            )
        },
        tick=10,
    )
    state = WorldState.model_validate({**state.model_dump(mode="json"), "scheduled_events": [schedule]})

    engine = RuleEngine()
    first = engine.resolve_scheduled(state, schedule, _context(actor_id))
    after_first = _replay(state, first.events)
    assert first.outcome.code == "production_quota_renewed"
    assert after_first.resources[source_id].credit_limit == Decimal("3")
    assert after_first.scheduled_events[0].status == "queued"
    assert after_first.scheduled_events[0].due_tick == 20
    assert after_first.scheduled_events[0].completed_runs == 1

    after_first = _replay(after_first, [TimeAdvancedEvent(to_tick=20)])
    second_schedule = after_first.scheduled_events[0]
    second = engine.resolve_scheduled(after_first, second_schedule, _context(actor_id))
    after_second = _replay(after_first, second.events)
    assert second.outcome.code == "production_quota_renewed"
    assert after_second.resources[source_id].credit_limit == Decimal("4")
    assert after_second.scheduled_events[0].status == "queued"
    assert after_second.scheduled_events[0].due_tick == 30

    after_second = _replay(after_second, [TimeAdvancedEvent(to_tick=30)])
    final_schedule = after_second.scheduled_events[0]
    final = engine.resolve_scheduled(after_second, final_schedule, _context(actor_id))
    after_final = _replay(after_second, final.events)
    assert final.outcome.code == "production_quota_exhausted"
    assert [event.type for event in final.events] == ["core.scheduled_event_completed.v1"]
    assert after_final.resources[source_id].credit_limit == Decimal("4")
    assert after_final.scheduled_events[0].status == "completed"
