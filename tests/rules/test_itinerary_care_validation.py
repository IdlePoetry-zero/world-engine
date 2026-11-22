from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.domain.components import (
    InventoryComponent,
    InventoryLot,
    NeedValue,
    NeedsComponent,
    PlaceComponent,
    PlaceRoute,
    ScheduledEventRecord,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import ResourceConsumedEvent
from world_engine.domain.models import (
    ResourceAccount,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import apply_event, state_hash, state_json
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)
from world_engine.rules import ConsumeIntent, RestIntent, RuleContext, RuleEngine, WaitIntent
from world_engine.rules.itinerary_care_validation import (
    ItineraryCareReceipt,
    evaluate_itinerary_care,
    validate_itinerary_care_batch,
)
from world_engine.simulation.contracts import LongActionPlan, SimulationContext
from world_engine.simulation.engine import SimulationEngine


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
FOREIGN_ACTOR_ID = UUID("40000000-0000-4000-8000-000000000005")
ORIGIN_ID = UUID("50000000-0000-4000-8000-000000000005")
DESTINATION_ID = UUID("60000000-0000-4000-8000-000000000006")
ROUTE_ID = UUID("70000000-0000-4000-8000-000000000007")
JOURNEY_ID = UUID("80000000-0000-4000-8000-000000000008")
FOOD_ACCOUNT_ID = UUID("90000000-0000-4000-8000-000000000009")
FOOD_SINK_ID = UUID("a0000000-0000-4000-8000-00000000000a")
FOOD_LOT_ID = UUID("b0000000-0000-4000-8000-00000000000b")
WATER_ACCOUNT_ID = UUID("c0000000-0000-4000-8000-00000000000c")
WATER_SINK_ID = UUID("d0000000-0000-4000-8000-00000000000d")
WATER_LOT_ID = UUID("e0000000-0000-4000-8000-00000000000e")
MEDICINE_ACCOUNT_ID = UUID("f0000000-0000-4000-8000-00000000000f")
MEDICINE_SINK_ID = UUID("11000000-0000-4000-8000-000000000011")
MEDICINE_LOT_ID = UUID("12000000-0000-4000-8000-000000000012")
FOREIGN_ACCOUNT_ID = UUID("13000000-0000-4000-8000-000000000013")
RULESET = "itinerary-care-test.v1"


def _state(
    *,
    with_medicine: bool = False,
    with_foreign_actor: bool = False,
    critical_hunger: bool = False,
) -> WorldState:
    route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=500,
    )
    needs = NeedsComponent(
        food=NeedValue(
            value=0 if critical_hunger else 60,
            critical_below=20,
            last_critical_damage_tick=0 if critical_hunger else None,
        ),
        water=NeedValue(value=65),
        rest=NeedValue(value=50),
    )
    lots = [
        InventoryLot(lot_id=FOOD_LOT_ID, account_id=FOOD_ACCOUNT_ID, resource_id="core.food", quantity="2"),
        InventoryLot(lot_id=WATER_LOT_ID, account_id=WATER_ACCOUNT_ID, resource_id="core.water", quantity="2"),
    ]
    resources = {
        FOOD_ACCOUNT_ID: ResourceAccount(resource_id="core.food", balance="2", owner_id=ACTOR_ID),
        FOOD_SINK_ID: ResourceAccount(resource_id="core.food", balance="0", account_type="consumption_sink"),
        WATER_ACCOUNT_ID: ResourceAccount(resource_id="core.water", balance="2", owner_id=ACTOR_ID),
        WATER_SINK_ID: ResourceAccount(resource_id="core.water", balance="0", account_type="consumption_sink"),
    }
    actor_components: dict[str, object] = {
        "needs": needs.model_dump(mode="json"),
        "inventory": InventoryComponent(lots=lots).model_dump(mode="json"),
    }
    if critical_hunger:
        actor_components["health"] = {"current": 80, "maximum": 100}
    if with_medicine:
        actor_components["health"] = {"current": 50, "maximum": 100}
        actor_components["inventory"] = InventoryComponent(lots=[
            *lots,
            InventoryLot(
                lot_id=MEDICINE_LOT_ID,
                account_id=MEDICINE_ACCOUNT_ID,
                resource_id="core.medicine",
                quantity="2",
            ),
        ]).model_dump(mode="json")
        resources[MEDICINE_ACCOUNT_ID] = ResourceAccount(
            resource_id="core.medicine", balance="2", owner_id=ACTOR_ID,
        )
        resources[MEDICINE_SINK_ID] = ResourceAccount(
            resource_id="core.medicine", balance="0", account_type="consumption_sink",
        )
    if with_foreign_actor:
        resources[FOREIGN_ACCOUNT_ID] = ResourceAccount(
            resource_id="core.food", balance="2", owner_id=FOREIGN_ACTOR_ID,
        )

    journey = TravelJourney(
        journey_id=JOURNEY_ID,
        actor_id=ACTOR_ID,
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        departed_tick=0,
        arrival_tick=500,
    )
    entities = {
        ORIGIN_ID: WorldEntity(
            id=ORIGIN_ID,
            kind="place",
            components={"place": PlaceComponent(routes=[route]).model_dump(mode="json")},
        ),
        DESTINATION_ID: WorldEntity(
            id=DESTINATION_ID,
            kind="place",
            components={"place": PlaceComponent().model_dump(mode="json")},
        ),
        ACTOR_ID: WorldEntity(
            id=ACTOR_ID,
            kind="character",
            location_id=None,
            components=actor_components,
        ),
    }
    if with_foreign_actor:
        entities[FOREIGN_ACTOR_ID] = WorldEntity(
            id=FOREIGN_ACTOR_ID,
            kind="character",
            location_id=ORIGIN_ID,
        )
    schedule = ScheduledEventRecord(
        id=travel_resolution_schedule_id(JOURNEY_ID),
        due_tick=500,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        payload={"journey_id": str(JOURNEY_ID)},
    )
    return WorldState(
        current_tick=100,
        entities=entities,
        resources=resources,
        travel_journeys=[journey],
        scheduled_events=[schedule],
    )


def _context(*, command_id: UUID = COMMAND_ID) -> RuleContext:
    return RuleContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=ACTOR_ID,
        ruleset_version=RULESET,
        simulation_command_id=command_id,
    )


def _evaluate(state: WorldState, intent, *, command_id: UUID = COMMAND_ID):
    return RuleEngine().evaluate(state, intent, _context(command_id=command_id))


def _validate(
    state: WorldState,
    events,
    *,
    world_id: UUID = WORLD_ID,
    actor_id: UUID = ACTOR_ID,
    receipt: object | None = None,
    authoritative_prestate_hash: str | None = None,
    return_projection: bool = False,
):
    return validate_itinerary_care_batch(
        state,
        events,
        world_id=world_id,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=actor_id,
        ruleset_version=RULESET,
        receipt=receipt,
        authoritative_prestate_hash=authoritative_prestate_hash,
        return_projection=return_projection,
    )


@pytest.mark.parametrize(
    "intent",
    [
        RestIntent(duration_ticks=120),
        ConsumeIntent(need="food", resource_id="core.food", quantity=Decimal("1")),
        ConsumeIntent(need="water", resource_id="core.water", quantity=Decimal("1")),
    ],
)
def test_accepts_exact_rule_generated_rest_and_one_unit_food_or_water_batches(intent):
    state = _state()
    before = state.model_dump(mode="json")
    result = _evaluate(state, intent)

    assert result.outcome.status == "succeeded"
    assert result.events[0].type == "core.travel_journey_changed.v1"
    assert _validate(state, result.events) is None
    assert state.model_dump(mode="json") == before


@pytest.mark.parametrize("quantity", [Decimal("0.1"), Decimal("0.5")])
def test_accepts_fractional_food_intake_at_or_above_restoration_threshold(quantity):
    state = _state()
    intent = ConsumeIntent(need="food", resource_id="core.food", quantity=quantity)
    result = _evaluate(state, intent)
    consumed = next(event for event in result.events if isinstance(event, ResourceConsumedEvent))

    assert result.outcome.status == "succeeded"
    assert consumed.flows[0].quantity == quantity
    assert _validate(state, result.events) is None


def test_rejects_batches_without_a_typed_same_actor_delay_at_the_head():
    state = _state()
    result = _evaluate(state, RestIntent(duration_ticks=120))

    with pytest.raises(InvalidEvent, match="begin with the acting traveler"):
        _validate(state, result.events[1:])
    with pytest.raises(InvalidEvent, match="typed core events"):
        _validate(state, [result.events[0].model_dump(mode="python"), *result.events[1:]])
    with pytest.raises(InvalidEvent, match="acting traveler"):
        _validate(state, result.events, actor_id=FOREIGN_ACTOR_ID)


def test_rejects_wrong_scope_and_nonmatching_event_json():
    state = _state()
    result = _evaluate(state, RestIntent(duration_ticks=120))

    with pytest.raises(InvalidEvent, match="trusted command scope"):
        _validate(state, result.events, world_id=uuid4())

    tampered = list(result.events)
    advance_index = next(index for index, event in enumerate(tampered) if isinstance(event, TimeAdvancedEvent))
    tampered[advance_index] = tampered[advance_index].model_copy(update={"to_tick": 221})
    with pytest.raises(InvalidEvent, match="matching clock advance"):
        _validate(state, tampered)


def test_wait_batch_does_not_pass_as_registered_rest_care():
    state = _state()
    result = _evaluate(state, WaitIntent(duration_ticks=120))

    with pytest.raises(InvalidEvent, match="exactly match ordinary rule evaluation"):
        _validate(state, result.events)


def test_partial_care_effects_are_not_authorized_for_automatic_travel_care():
    state = _state(critical_hunger=True)
    result = _evaluate(state, RestIntent(duration_ticks=86_400))

    assert result.outcome.status == "partial"
    with pytest.raises(InvalidEvent, match="fully successful"):
        _validate(state, result.events)


def test_rejects_health_medicine_and_non_unit_or_foreign_resource_consumption():
    state = _state(with_medicine=True, with_foreign_actor=True)
    medicine = _evaluate(
        state,
        ConsumeIntent(need="health", resource_id="core.medicine", quantity=Decimal("1")),
    )
    with pytest.raises(InvalidEvent, match="food or water flow"):
        _validate(state, medicine.events)

    food = _evaluate(
        state,
        ConsumeIntent(need="food", resource_id="core.food", quantity=Decimal("1")),
    )
    consumed_index = next(
        index for index, event in enumerate(food.events) if isinstance(event, ResourceConsumedEvent)
    )
    consumed = food.events[consumed_index]
    flow = consumed.flows[0]

    over_cap = consumed.model_validate(consumed.model_dump(mode="python") | {
        "flows": [flow.model_dump(mode="python") | {"quantity": "1.5"}],
    })
    over_cap_events = list(food.events)
    over_cap_events[consumed_index] = over_cap
    with pytest.raises(InvalidEvent, match="between 0.1 and one resource unit"):
        _validate(state, over_cap_events)

    foreign = consumed.model_validate(consumed.model_dump(mode="python") | {
        "flows": [flow.model_dump(mode="python") | {"holder_account_id": str(FOREIGN_ACCOUNT_ID)}],
    })
    foreign_events = list(food.events)
    foreign_events[consumed_index] = foreign
    with pytest.raises(InvalidEvent, match="acting traveler's resource account"):
        _validate(state, foreign_events)


def test_rejects_purchase_or_additional_unrelated_resource_events():
    state = _state()
    result = _evaluate(state, RestIntent(duration_ticks=120))
    purchase = ResourceTransferredEvent(
        from_account_id=FOOD_ACCOUNT_ID,
        to_account_id=FOOD_SINK_ID,
        resource_id="core.food",
        quantity=Decimal("1"),
    )

    with pytest.raises(InvalidEvent, match="exactly match ordinary rule evaluation"):
        _validate(state, [*result.events, purchase])


def test_native_success_receipt_skips_only_the_duplicate_rule_evaluation(monkeypatch):
    state = _state()
    intent = RestIntent(duration_ticks=120)
    resolution, receipt = evaluate_itinerary_care(state, intent, _context())
    assert isinstance(receipt, ItineraryCareReceipt)

    original_evaluate = RuleEngine.evaluate
    calls = 0

    def counted_evaluate(engine, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_evaluate(engine, *args, **kwargs)

    monkeypatch.setattr(RuleEngine, "evaluate", counted_evaluate)
    assert _validate(
        state,
        resolution.events,
        receipt=receipt,
        authoritative_prestate_hash=state_hash(state),
    ) is None
    assert calls == 0


def test_native_itinerary_receipt_seals_reduced_projection_and_exact_event_ticks():
    state = _state()
    ordinary = _evaluate(state, RestIntent(duration_ticks=120))
    assert ordinary._validated_projection is None

    resolution, receipt = evaluate_itinerary_care(
        state, RestIntent(duration_ticks=120), _context(),
    )
    assert isinstance(receipt, ItineraryCareReceipt)
    assert receipt.projected_state is not None

    expected = state
    for event in resolution.events:
        expected = apply_event(expected, event)
    assert state_hash(receipt.projected_state) == state_hash(expected)
    assert receipt.event_ticks == (state.current_tick, *(
        state.current_tick + resolution.elapsed_ticks for _ in resolution.events[1:]
    ))
    assert "_validated_projection" not in resolution.model_dump(mode="json")
    assert _validate(
        state,
        resolution.events,
        receipt=receipt,
        authoritative_prestate_hash=state_hash(state),
        return_projection=True,
    ) is receipt
    assert receipt.matches_projected_payload(state_json(receipt.projected_state))


def test_projection_order_binding_rejects_nested_mapping_reorder():
    state = _state()
    _, receipt = evaluate_itinerary_care(
        state, RestIntent(duration_ticks=120), _context(),
    )
    assert isinstance(receipt, ItineraryCareReceipt)
    projected = receipt.projected_state
    assert projected is not None
    original_hash = state_hash(projected)
    original_resources = dict(projected.resources)
    projected.resources.clear()
    projected.resources.update(reversed(list(original_resources.items())))

    assert state_hash(projected) == original_hash
    assert not receipt.matches_projected_payload(state_json(projected))


def test_untrusted_or_canonically_mismatched_receipts_fall_back_to_replay(monkeypatch):
    state = _state()
    resolution, receipt = evaluate_itinerary_care(state, RestIntent(duration_ticks=120), _context())
    assert receipt is not None

    original_evaluate = RuleEngine.evaluate
    calls = 0

    def counted_evaluate(engine, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_evaluate(engine, *args, **kwargs)

    monkeypatch.setattr(RuleEngine, "evaluate", counted_evaluate)
    assert _validate(
        state,
        resolution.events,
        receipt=object(),
        authoritative_prestate_hash=state_hash(state),
    ) is None
    assert _validate(
        state,
        resolution.events,
        receipt=receipt,
        authoritative_prestate_hash="0" * 64,
    ) is None
    assert calls == 2

    with pytest.raises(TypeError, match="only be issued"):
        ItineraryCareReceipt(
            capability=object(),
            prestate_hash=state_hash(state),
            ordered_prestate_digest="0" * 64,
            context_serialization="{}",
            intent_serialization="{}",
            elapsed_ticks=120,
            event_serializations=(),
        )


def test_context_mismatch_and_same_hash_reordered_state_fall_back_to_replay(monkeypatch):
    state = _state()
    intent = RestIntent(duration_ticks=120)
    alternate_context = _context().model_copy(update={"accusation_repair_requested_by": FOREIGN_ACTOR_ID})
    resolution, context_receipt = evaluate_itinerary_care(state, intent, alternate_context)

    reordered_state = state.model_copy(
        update={"resources": dict(reversed(list(state.resources.items())))}
    )
    assert state_hash(reordered_state) == state_hash(state)

    original_evaluate = RuleEngine.evaluate
    calls = 0

    def counted_evaluate(engine, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_evaluate(engine, *args, **kwargs)

    monkeypatch.setattr(RuleEngine, "evaluate", counted_evaluate)
    assert _validate(
        state,
        resolution.events,
        receipt=context_receipt,
        authoritative_prestate_hash=state_hash(state),
    ) is None
    assert _validate(
        reordered_state,
        resolution.events,
        receipt=context_receipt,
        authoritative_prestate_hash=state_hash(reordered_state),
    ) is None
    assert calls == 2


def test_reordered_or_changed_event_batch_never_passes_receipt_binding():
    state = _state()
    resolution, receipt = evaluate_itinerary_care(
        state, RestIntent(duration_ticks=120), _context(),
    )
    assert receipt is not None

    reordered = [resolution.events[0], resolution.events[2], resolution.events[1]]
    with pytest.raises(InvalidEvent):
        _validate(
            state,
            reordered,
            receipt=receipt,
            authoritative_prestate_hash=state_hash(state),
        )

    tampered = list(resolution.events)
    advanced_index = next(
        index for index, event in enumerate(tampered) if isinstance(event, TimeAdvancedEvent)
    )
    advanced = tampered[advanced_index]
    tampered[advanced_index] = TimeAdvancedEvent.model_validate(
        advanced.model_dump(mode="python") | {"to_tick": advanced.to_tick + 1}
    )
    with pytest.raises(InvalidEvent):
        _validate(
            state,
            tampered,
            receipt=receipt,
            authoritative_prestate_hash=state_hash(state),
        )


def test_custom_simulation_rule_engine_does_not_receive_native_receipt(monkeypatch):
    class CustomRuleEngine(RuleEngine):
        def evaluate(self, state, intent, context):
            return RuleEngine().evaluate(state, intent, context)

    def native_wrapper_must_not_run(*_args, **_kwargs):
        raise AssertionError("custom rule engines cannot receive native receipts")

    monkeypatch.setattr("world_engine.simulation.engine.evaluate_itinerary_care", native_wrapper_must_not_run)
    state = _state()
    plan = LongActionPlan(
        actor_id=ACTOR_ID,
        started_tick=state.current_tick,
        target_tick=1000,
        activity="travel",
        route_id=ROUTE_ID,
    )
    context = SimulationContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        command_actor_id=ACTOR_ID,
        world_seed="itinerary-care-test",
        state_version=0,
        ruleset_version=RULESET,
        itinerary_journey_id=JOURNEY_ID,
        itinerary_auto_care=True,
    )

    result = SimulationEngine(rule_engine=CustomRuleEngine())._resolve_itinerary_auto_care(
        state,
        plan,
        context,
        state.travel_journeys[0],
        RestIntent(duration_ticks=120),
        120,
    )
    assert result.outcome == "advanced"
    assert result._itinerary_care_receipt is None
    assert "_itinerary_care_receipt" not in result.model_dump(mode="json")
