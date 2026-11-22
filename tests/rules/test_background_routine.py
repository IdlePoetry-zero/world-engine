from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from world_engine.domain.components import (
    DailyRoutineComponent,
    DailyRoutineWindow,
    InventoryComponent,
    InventoryLot,
    NeedsComponent,
)
from world_engine.domain.background_routine import advance_background_routine
from world_engine.domain.gameplay_events import BackgroundRoutineAdvancedEvent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, ledger_postings
from world_engine.rules import RuleContext, RuleEngine


DAY = 86_400
WINDOW = 30 * DAY


def _context(actor_id):
    return RuleContext(
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _state(*, supplied=True, actor_id=None):
    place_id = uuid4()
    owner_actor_id = actor_id or uuid4()
    people_ids = [uuid4(), uuid4()]
    resources = {}
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", name="Home", components={"place": {"place_type": "settlement"}})
    }
    for person_id in people_ids:
        food_account_id, water_account_id = uuid4(), uuid4()
        food_balance = Decimal("100") if supplied else Decimal("0")
        water_balance = Decimal("100") if supplied else Decimal("0")
        lots = []
        if supplied:
            lots = [
                InventoryLot(
                    lot_id=uuid4(), account_id=food_account_id, resource_id="core.food", quantity="100"
                ),
                InventoryLot(
                    lot_id=uuid4(), account_id=water_account_id, resource_id="core.water", quantity="100"
                ),
            ]
        entities[person_id] = WorldEntity(
            id=person_id,
            kind="character",
            name="Resident",
            location_id=place_id,
            components={
                "needs": {
                    "food": {"value": 60, "critical_below": 20},
                    "water": {"value": 60, "critical_below": 20},
                    "rest": {"value": 50},
                },
                "inventory": InventoryComponent(lots=lots),
                "daily_routine": DailyRoutineComponent(windows=[
                    DailyRoutineWindow(
                        window_id=uuid4(),
                        start_second=20 * 3600,
                        duration_seconds=4 * 3600,
                        activity="rest",
                        place_id=place_id,
                    )
                ]),
            },
        )
        resources[food_account_id] = ResourceAccount(
            resource_id="core.food", balance=food_balance, owner_id=person_id, account_type="holder"
        )
        resources[water_account_id] = ResourceAccount(
            resource_id="core.water", balance=water_balance, owner_id=person_id, account_type="holder"
        )
    food_sink_id, water_sink_id = uuid4(), uuid4()
    resources[food_sink_id] = ResourceAccount(resource_id="core.food", balance="0", account_type="consumption_sink")
    resources[water_sink_id] = ResourceAccount(resource_id="core.water", balance="0", account_type="consumption_sink")
    state = WorldState(
        entities=entities,
        resources=resources,
        player_id=people_ids[0],
        metadata={"resource_roles": {"food_resource_id": "core.food", "water_resource_id": "core.water"}},
    )
    return state, owner_actor_id, people_ids


def _apply(state, events):
    result = state
    for event in events:
        result = apply_event(result, event)
    return result


def _need_values(state, entity_id):
    needs = state.entities[entity_id].components["needs"]
    return {
        name: {
            key: value
            for key, value in item.items()
            if key not in {"revision"}
        }
        for name, item in needs.items()
        if isinstance(item, dict)
    }


def test_observer_window_batches_food_water_ledgers_and_daily_rest():
    state, owner_actor_id, people_ids = _state()
    resolution = RuleEngine().resolve_background_window(
        state,
        from_tick=0,
        to_tick=WINDOW,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )

    assert resolution.outcome.status == "succeeded"
    assert [event.type for event in resolution.events] == [
        "core.time_advanced.v1",
        "core.background_routine_advanced.v1",
    ]
    batch = resolution.events[1]
    assert isinstance(batch, BackgroundRoutineAdvancedEvent)
    assert [item.entity_id for item in batch.expected_agents] == sorted(people_ids, key=str)
    assert all(item.food and item.food.action_count > 0 for item in batch.expected_agents)
    assert all(item.water and item.water.action_count > 0 for item in batch.expected_agents)
    postings = ledger_postings(batch)
    for resource_id in {item.resource_id for item in postings}:
        assert sum((item.delta for item in postings if item.resource_id == resource_id), Decimal("0")) == 0

    result = _apply(state, resolution.events)
    for person_id in people_ids:
        before = state.entities[person_id].components["needs"]
        after = result.entities[person_id].components["needs"]
        assert after["food"]["value"] > 0
        assert after["water"]["value"] > 0
        assert after["rest"]["value"] != before["rest"]["value"]
        assert result.entities[person_id].components["inventory"]["revision"] == 2


def test_routine_results_match_two_adjacent_windows():
    state, owner_actor_id, people_ids = _state()
    engine = RuleEngine()
    one = engine.resolve_background_window(
        state,
        from_tick=0,
        to_tick=WINDOW,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )
    one_state = _apply(state, one.events)

    first = engine.resolve_background_window(
        state,
        from_tick=0,
        to_tick=15 * DAY,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )
    half_state = _apply(state, first.events)
    second = engine.resolve_background_window(
        half_state,
        from_tick=15 * DAY,
        to_tick=WINDOW,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )
    split_state = _apply(half_state, second.events)

    for person_id in people_ids:
        assert _need_values(one_state, person_id) == _need_values(split_state, person_id)
        one_lots = one_state.entities[person_id].components["inventory"]["lots"]
        split_lots = split_state.entities[person_id].components["inventory"]["lots"]
        assert [(lot["lot_id"], lot["quantity"]) for lot in one_lots] == [
            (lot["lot_id"], lot["quantity"]) for lot in split_lots
        ]
        assert one_state.entities[person_id].components["inventory"]["revision"] == 2
        assert split_state.entities[person_id].components["inventory"]["revision"] == 3
    for account_id in one_state.resources:
        assert one_state.resources[account_id].balance == split_state.resources[account_id].balance


def test_routine_window_refuses_to_cross_deprivation_after_stock_runs_out():
    state, owner_actor_id, _people_ids = _state(supplied=False)
    engine = RuleEngine()
    resolution = engine.resolve_background_window(
        state,
        from_tick=0,
        to_tick=WINDOW,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )

    assert resolution.outcome.status == "rejected"
    assert resolution.outcome.code == "background_deprivation_pending"
    assert 0 < resolution.outcome.details["boundary_tick"] < WINDOW
    assert resolution.events == []
    assert resolution.elapsed_ticks == 0


def test_routine_pending_boundary_is_global_earliest_not_first_uuid():
    state, owner_actor_id, people_ids = _state(supplied=False)
    lower_id, higher_id = sorted(people_ids, key=str)
    higher_person = state.entities[higher_id]
    higher_components = dict(higher_person.components)
    higher_needs = NeedsComponent.model_validate(higher_components["needs"])
    higher_components["needs"] = higher_needs.model_copy(update={
        "water": higher_needs.water.model_copy(update={"value": 40}),
    }).model_dump(mode="json")
    entities = dict(state.entities)
    entities[higher_id] = higher_person.model_copy(update={"components": higher_components})
    state = WorldState.model_validate({**state.model_dump(mode="python"), "entities": entities})

    def boundary_for(person_id):
        person = state.entities[person_id]
        needs = NeedsComponent.model_validate(person.components["needs"])
        inventory = InventoryComponent.model_validate(person.components["inventory"])
        routine = DailyRoutineComponent.model_validate(person.components["daily_routine"])
        return advance_background_routine(
            needs,
            inventory=inventory,
            resources=state.resources,
            metadata=state.metadata,
            routine=routine,
            location_id=person.location_id,
            from_tick=0,
            to_tick=WINDOW,
            day_seconds=state.calendar.day_seconds,
        ).deprivation_boundary

    later_boundary = boundary_for(lower_id)
    earlier_boundary = boundary_for(higher_id)
    assert higher_id > lower_id
    assert earlier_boundary is not None and later_boundary is not None
    assert earlier_boundary < later_boundary

    resolution = RuleEngine().resolve_background_window(
        state,
        from_tick=0,
        to_tick=WINDOW,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )

    assert resolution.outcome.status == "rejected"
    assert resolution.outcome.code == "background_deprivation_pending"
    assert resolution.outcome.details["boundary_tick"] == earlier_boundary
    assert resolution.events == []
    assert resolution.elapsed_ticks == 0


def test_routine_window_ending_at_deprivation_boundary_reports_boundary():
    state, owner_actor_id, _people_ids = _state(supplied=False)
    engine = RuleEngine()
    longer = engine.resolve_background_window(
        state,
        from_tick=0,
        to_tick=WINDOW,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )
    boundary_tick = longer.outcome.details["boundary_tick"]

    exact = engine.resolve_background_window(
        state,
        from_tick=0,
        to_tick=boundary_tick,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )

    assert exact.outcome.status == "succeeded"
    assert exact.outcome.details["boundary_tick"] == boundary_tick
    assert exact.elapsed_ticks == boundary_tick
    assert exact.events[-1].type == "core.background_routine_advanced.v1"
    exact_state = _apply(state, exact.events)
    assert exact_state.current_tick == boundary_tick


def test_routine_care_skips_expired_fifo_lots_and_posts_water_before_food():
    state, owner_actor_id, people_ids = _state()
    person_id = people_ids[0]
    person = state.entities[person_id]
    needs = NeedsComponent.model_validate(person.components["needs"])
    needs = needs.model_copy(update={
        "food": needs.food.model_copy(update={"value": 10}),
        "water": needs.water.model_copy(update={"value": 10}),
    })
    food_account_id = next(
        account_id for account_id, account in state.resources.items()
        if account.owner_id == person_id and account.resource_id == "core.food"
    )
    water_account_id = next(
        account_id for account_id, account in state.resources.items()
        if account.owner_id == person_id and account.resource_id == "core.water"
    )
    expired_lot_id, valid_food_lot_id = uuid4(), uuid4()
    inventory = InventoryComponent(lots=[
        InventoryLot(
            lot_id=expired_lot_id,
            account_id=food_account_id,
            resource_id="core.food",
            quantity="1",
            expiry_tick=0,
        ),
        InventoryLot(
            lot_id=valid_food_lot_id,
            account_id=food_account_id,
            resource_id="core.food",
            quantity="99",
        ),
        InventoryLot(
            lot_id=uuid4(),
            account_id=water_account_id,
            resource_id="core.water",
            quantity="100",
        ),
    ])
    components = dict(person.components)
    components["needs"] = needs.model_dump(mode="json")
    components["inventory"] = inventory.model_dump(mode="json")
    entities = dict(state.entities)
    entities[person_id] = person.model_copy(update={"components": components})
    state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": entities,
    })

    resolution = RuleEngine().resolve_background_window(
        state,
        from_tick=0,
        to_tick=1,
        context=_context(owner_actor_id),
        choices=(),
        include_actor=True,
    )
    event = resolution.events[-1]
    care = next(item for item in event.expected_agents if item.entity_id == person_id)

    assert care.water is not None and care.water.action_count == 1
    assert care.food is not None and care.food.action_count == 1
    assert [item.lot_id for item in care.food.consumption_lots] == [valid_food_lot_id]
    assert expired_lot_id not in {item.lot_id for item in care.food.consumption_lots}
    person_postings = [
        posting for posting in ledger_postings(event)
        if posting.account_id in {food_account_id, water_account_id}
    ]
    assert [posting.resource_id for posting in person_postings] == [
        "core.water", "core.food"
    ]
