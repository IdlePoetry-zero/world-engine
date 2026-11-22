from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import (
    InventoryComponent,
    InventoryLot,
    KnowledgeRecord,
    MarketComponent,
    MarketOffer,
    ProductionQuotaPolicy,
    ScheduledEventRecord,
    TreasuryComponent,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.reducers import apply_event
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.simulation.contracts import (
    LongActionPlan,
    ResolutionMetrics,
    RoutineWorkSchedule,
    WorkPurchaseAffordabilityReceipt,
    WorkPurchaseTarget,
)
from world_engine.simulation.engine import SimulationContext, SimulationEngine
from world_engine.simulation.work_purchase import observe_work_purchase_quote


DAY = 86_400
SHIFT = 3_600
WORKER_ID = UUID("10000000-0000-4000-8000-000000000001")
EMPLOYER_ID = UUID("20000000-0000-4000-8000-000000000002")
WORKPLACE_ID = UUID("30000000-0000-4000-8000-000000000003")
POSTING_ID = UUID("40000000-0000-4000-8000-000000000004")
SOURCE_ID = UUID("50000000-0000-4000-8000-000000000005")
WORKER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
EMPLOYER_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")
SELLER_ID = UUID("80000000-0000-4000-8000-000000000008")
STOCK_ACCOUNT_ID = UUID("90000000-0000-4000-8000-000000000009")
LISTING_ID = UUID("a0000000-0000-4000-8000-00000000000a")
LOT_ID = UUID("b0000000-0000-4000-8000-00000000000b")
RESOURCE_ID = "core.silver"
GOODS_ID = "core.grain"


def _target(**updates) -> WorkPurchaseTarget:
    return WorkPurchaseTarget.model_validate({
        "market_id": WORKPLACE_ID,
        "listing_id": LISTING_ID,
        "resource_id": GOODS_ID,
        "currency_resource_id": RESOURCE_ID,
        "seller_id": SELLER_ID,
        "quantity": "3",
        **updates,
    })


def _state(
    *, tick=0, price=5, stock_quantity="4", minimum_quantity="2", worker_balance="5", scheduled_events=(),
):
    source = OccupationSource(kind="event", reference_id=str(SOURCE_ID))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, WORKER_ID, POSTING_ID),
        actor_id=WORKER_ID,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Registry clerk",
        started_tick=0,
        revision=1,
        source=source,
    )
    entities = {
        WORKER_ID: WorldEntity(
            id=WORKER_ID, kind="character", status="active", location_id=WORKPLACE_ID,
        ),
        EMPLOYER_ID: WorldEntity(
            id=EMPLOYER_ID, kind="organization", status="active",
            components={"treasury": TreasuryComponent(accounts={RESOURCE_ID: EMPLOYER_ACCOUNT_ID}).model_dump(mode="json")},
        ),
        WORKPLACE_ID: WorldEntity(
            id=WORKPLACE_ID, kind="place", status="active",
            components={"market": MarketComponent(
                currency_resource_id=RESOURCE_ID,
                offers=[MarketOffer(
                    listing_id=LISTING_ID,
                    resource_id=GOODS_ID,
                    stock_account_id=STOCK_ACCOUNT_ID,
                    seller_id=SELLER_ID,
                    unit_price_minor=price,
                    minimum_quantity=minimum_quantity,
                )],
            ).model_dump(mode="json")},
        ),
        SELLER_ID: WorldEntity(
            id=SELLER_ID, kind="organization", status="active", location_id=WORKPLACE_ID,
            components={"inventory": InventoryComponent(lots=[InventoryLot(
                lot_id=LOT_ID,
                account_id=STOCK_ACCOUNT_ID,
                resource_id=GOODS_ID,
                quantity=stock_quantity,
            )]).model_dump(mode="json")},
        ),
    }
    return WorldState(
        current_tick=tick,
        calendar={"day_seconds": DAY},
        entities=entities,
        resources={
            WORKER_ACCOUNT_ID: ResourceAccount(resource_id=RESOURCE_ID, balance=worker_balance, owner_id=WORKER_ID),
            EMPLOYER_ACCOUNT_ID: ResourceAccount(resource_id=RESOURCE_ID, balance="100", owner_id=EMPLOYER_ID),
            STOCK_ACCOUNT_ID: ResourceAccount(
                resource_id=GOODS_ID, balance=stock_quantity, owner_id=SELLER_ID,
            ),
        },
        work_postings=[WorkPosting(
            posting_id=POSTING_ID, employer_id=EMPLOYER_ID, location_id=WORKPLACE_ID,
            title="Registry clerk", slots=1, valid_from_tick=0,
        )],
        work_assignments=[assignment],
        metadata={"work_shift_terms": [WorkShiftTerms(
            posting_id=POSTING_ID, duration_seconds=SHIFT, resource_id=RESOURCE_ID, wage="12.50",
        ).model_dump(mode="json")]},
        scheduled_events=list(scheduled_events),
    )


def _plan(*, purchase_target=None, balance_target=None, target_tick=3 * DAY):
    return LongActionPlan(
        actor_id=WORKER_ID,
        started_tick=0,
        target_tick=target_tick,
        activity="work",
        work_schedule=RoutineWorkSchedule(
            assignment_id=occupation_assignment_id(
                OccupationSource(kind="event", reference_id=str(SOURCE_ID)), WORKER_ID, POSTING_ID,
            ),
            first_shift_tick=0,
            repeat_every_days=1,
            balance_target=balance_target,
            purchase_target=purchase_target,
        ),
    )


def _context(version):
    return SimulationContext(
        world_id=UUID("c0000000-0000-4000-8000-00000000000c"),
        branch_id=UUID("d0000000-0000-4000-8000-00000000000d"),
        command_id=uuid4(),
        world_seed="work-purchase-test",
        state_version=version,
    )


def _apply(state, events):
    for event in events:
        state = apply_event(state, event)
    return state


def test_purchase_target_is_optional_and_exclusive_with_balance_target():
    legacy = RoutineWorkSchedule(assignment_id=uuid4(), first_shift_tick=0, repeat_every_days=1)
    assert "purchase_target" not in legacy.model_dump(mode="json")
    with pytest.raises(ValidationError, match="both a balance_target and purchase_target"):
        RoutineWorkSchedule(
            assignment_id=uuid4(), first_shift_tick=0, repeat_every_days=1,
            balance_target="10", purchase_target=_target(),
        )
    with pytest.raises(ValidationError):
        _target(quantity="0")


def test_affordability_receipt_is_exact_validated_and_legacy_metrics_omit_it():
    receipt = WorkPurchaseAffordabilityReceipt(
        listing_id=LISTING_ID,
        quantity="3",
        currency_resource_id=RESOURCE_ID,
        total_price_minor="15",
        wage_balance="17.50",
        confirmed_tick=SHIFT,
    )
    assert receipt.model_dump(mode="json") == {
        "listing_id": str(LISTING_ID),
        "quantity": "3",
        "currency_resource_id": RESOURCE_ID,
        "total_price_minor": "15",
        "wage_balance": "17.50",
        "confirmed_tick": SHIFT,
    }
    with pytest.raises(ValidationError, match="integer minor-unit"):
        WorkPurchaseAffordabilityReceipt(
            listing_id=LISTING_ID, quantity="3", currency_resource_id=RESOURCE_ID,
            total_price_minor="15.5", wage_balance="20", confirmed_tick=SHIFT,
        )
    with pytest.raises(ValidationError, match="must cover"):
        WorkPurchaseAffordabilityReceipt(
            listing_id=LISTING_ID, quantity="3", currency_resource_id=RESOURCE_ID,
            total_price_minor="15", wage_balance="14.99", confirmed_tick=SHIFT,
        )
    with pytest.raises(ValidationError):
        WorkPurchaseAffordabilityReceipt(
            listing_id=LISTING_ID, quantity="0", currency_resource_id=RESOURCE_ID,
            total_price_minor="0", wage_balance="0", confirmed_tick=SHIFT,
        )
    with pytest.raises(ValidationError):
        WorkPurchaseAffordabilityReceipt(
            listing_id=LISTING_ID, quantity="3", currency_resource_id=RESOURCE_ID,
            total_price_minor="15", wage_balance="20", confirmed_tick=True,
        )

    metrics = ResolutionMetrics(
        from_tick=0, to_tick=0, ticks_advanced=0, integration_boundary="target_reached",
        event_count=0, depth="deep", speed="balanced",
    )
    assert "work_purchase_affordability" not in metrics.model_dump(mode="json")


def test_preexisting_wage_balance_completes_without_events_and_returns_receipt():
    state = _state(tick=120, worker_balance="20")
    resolution = SimulationEngine().plan_window(
        state, _plan(purchase_target=_target()), _context(0),
    )

    assert resolution.outcome == "completed"
    assert resolution.metrics.integration_boundary == "work_purchase_target_affordable"
    assert resolution.events == ()
    assert resolution.next_tick == state.current_tick
    receipt = resolution.metrics.work_purchase_affordability
    assert receipt == WorkPurchaseAffordabilityReceipt(
        listing_id=LISTING_ID,
        quantity="3",
        currency_resource_id=RESOURCE_ID,
        total_price_minor="15",
        wage_balance="20",
        confirmed_tick=120,
    )
    assert resolution.model_dump(mode="json")["metrics"]["work_purchase_affordability"] == (
        receipt.model_dump(mode="json")
    )


def test_quote_uses_current_listing_price_and_fifo_eligible_stock():
    state = _state(price=5, stock_quantity="3")
    assert observe_work_purchase_quote(state, _target()) == Decimal("15")

    raw = state.model_dump(mode="json")
    raw["entities"][str(WORKPLACE_ID)]["components"]["market"]["offers"][0]["unit_price_minor"] = 7
    repriced = WorldState.model_validate(raw)
    assert observe_work_purchase_quote(repriced, _target()) == Decimal("21")


def test_quote_rejects_non_integral_minor_unit_payment():
    state = _state(price=5, minimum_quantity="0.1")
    with pytest.raises(ValueError, match="integer minor-unit payment"):
        observe_work_purchase_quote(state, _target(quantity="0.5"))


@pytest.mark.parametrize("target,offers,stock", [
    (_target(), [], "4"),
    (_target(seller_id=UUID("e0000000-0000-4000-8000-00000000000e")), None, "4"),
    (_target(resource_id="core.water"), None, "4"),
    (_target(quantity="1"), None, "4"),
    (_target(), None, "2"),
    (_target(market_id=UUID("e0000000-0000-4000-8000-00000000000e")), None, "4"),
    (_target(currency_resource_id="core.water"), None, "4"),
])
def test_unavailable_or_mismatched_target_pauses_work(target, offers, stock):
    state = _state(stock_quantity=stock)
    if offers is not None:
        raw = state.model_dump(mode="json")
        raw["entities"][str(WORKPLACE_ID)]["components"]["market"]["offers"] = offers
        state = WorldState.model_validate(raw)

    resolution = SimulationEngine().plan_window(state, _plan(purchase_target=target), _context(0))

    assert resolution.outcome == "stopped"
    assert resolution.metrics.integration_boundary == "work_purchase_target_unavailable"
    assert resolution.interruption.reason_code == "work_purchase_target_unavailable"
    assert resolution.next_tick == state.current_tick
    assert resolution.events == ()


@pytest.mark.parametrize("hidden_entity_id", [WORKPLACE_ID, SELLER_ID])
def test_hidden_market_or_seller_must_be_observed_by_worker(hidden_entity_id):
    state = _state()
    raw = state.model_dump(mode="json")
    raw["entities"][str(hidden_entity_id)]["components"]["hidden"] = True
    hidden_state = WorldState.model_validate(raw)
    plan = _plan(purchase_target=_target())

    unavailable = SimulationEngine().plan_window(hidden_state, plan, _context(0))
    assert unavailable.outcome == "stopped"
    assert unavailable.metrics.integration_boundary == "work_purchase_target_unavailable"

    raw = hidden_state.model_dump(mode="json")
    raw["knowledge"] = [KnowledgeRecord(
        id=uuid4(),
        holder_id=WORKER_ID,
        subject_id=hidden_entity_id,
        claim="The worker has observed this market participant.",
        received_tick=0,
        kind="observation",
    ).model_dump(mode="json")]
    observed_state = WorldState.model_validate(raw)
    visible = SimulationEngine().plan_window(observed_state, plan, _context(1))
    assert visible.outcome == "completed"
    assert visible.metrics.integration_boundary == "work_purchase_target_affordable"


def test_hidden_seller_is_rechecked_after_a_paid_shift():
    state = _state(price=7)
    plan = _plan(purchase_target=_target())
    engine = SimulationEngine()
    first = engine.plan_window(state, plan, _context(0))
    assert first.outcome == "advanced"
    state = _apply(state, first.events)

    raw = state.model_dump(mode="json")
    raw["entities"][str(SELLER_ID)]["components"]["hidden"] = True
    hidden = WorldState.model_validate(raw)
    paused = engine.plan_window(hidden, plan, _context(1))
    assert paused.outcome == "stopped"
    assert paused.metrics.integration_boundary == "work_purchase_target_unavailable"
    assert paused.events == ()


def test_purchase_target_finishes_after_exact_affordable_shift_without_trading():
    state = _state(price=5)
    resolution = SimulationEngine().plan_window(
        state, _plan(purchase_target=_target()), _context(0),
    )

    assert resolution.outcome == "completed"
    assert resolution.metrics.integration_boundary == "work_purchase_target_affordable"
    paid, = [event for event in resolution.events if isinstance(event, WorkShiftPaidEvent)]
    assert paid.plan.shift_started_tick == 0
    assert resolution.next_tick == SHIFT
    assert resolution.metrics.work_purchase_affordability == WorkPurchaseAffordabilityReceipt(
        listing_id=LISTING_ID,
        quantity="3",
        currency_resource_id=RESOURCE_ID,
        total_price_minor="15",
        wage_balance="17.50",
        confirmed_tick=SHIFT,
    )
    assert all("trade" not in event.type for event in resolution.events)
    settled = _apply(state, resolution.events)
    assert settled.resources[WORKER_ACCOUNT_ID].balance == Decimal("17.50")
    assert state.resources[STOCK_ACCOUNT_ID].balance == Decimal("4")


def test_repricing_is_checked_at_the_next_settled_boundary():
    state = _state(price=7)
    plan = _plan(purchase_target=_target())
    engine = SimulationEngine()
    first = engine.plan_window(state, plan, _context(0))
    assert first.outcome == "advanced"
    state = _apply(state, first.events)
    assert state.resources[WORKER_ACCOUNT_ID].balance == Decimal("17.50")
    assert observe_work_purchase_quote(state, _target()) == Decimal("21")

    raw = state.model_dump(mode="json")
    raw["entities"][str(WORKPLACE_ID)]["components"]["market"]["offers"][0]["unit_price_minor"] = 5
    repriced = WorldState.model_validate(raw)
    finished = engine.plan_window(repriced, plan, _context(1))

    assert finished.outcome == "completed"
    assert finished.metrics.integration_boundary == "work_purchase_target_affordable"
    assert finished.events == ()
    assert finished.metrics.work_purchase_affordability == WorkPurchaseAffordabilityReceipt(
        listing_id=LISTING_ID,
        quantity="3",
        currency_resource_id=RESOURCE_ID,
        total_price_minor="15",
        wage_balance="17.50",
        confirmed_tick=SHIFT,
    )


def test_same_tick_scheduled_event_runs_before_affordability_completes_job():
    schedule_id = UUID("f0000000-0000-4000-8000-00000000000f")
    quota_source_id = UUID("11000000-0000-4000-8000-000000000011")
    state = _state(scheduled_events=[ScheduledEventRecord(
        id=schedule_id,
        due_tick=SHIFT,
        phase="environment",
        handler_id="production.quota_renewal",
        handler_version="1.0.0",
        payload={"producer_id": str(WORKPLACE_ID), "quota_id": "quota.daily"},
    )])
    raw = state.model_dump(mode="json")
    quota = ProductionQuotaPolicy(
        quota_id="quota.daily",
        resource_id=GOODS_ID,
        source_account_id=quota_source_id,
        period_ticks=SHIFT,
        per_period="1",
        maximum_renewal="1",
        base_credit_limit="1",
        issued_total="1",
        last_issued_period=0,
    )
    raw["entities"][str(WORKPLACE_ID)]["components"]["production"] = {"quotas": [quota.model_dump(mode="json")]}
    raw["resources"][str(quota_source_id)] = ResourceAccount(
        resource_id=GOODS_ID, balance="0", owner_id=WORKPLACE_ID,
        account_type="production_source", credit_limit="2",
    ).model_dump(mode="json")
    state = WorldState.model_validate(raw)
    plan = _plan(purchase_target=_target(), target_tick=SHIFT)
    engine = SimulationEngine()

    work = engine.plan_window(state, plan, _context(0))
    assert work.outcome == "advanced"
    assert work.metrics.integration_boundary == "work_shift_complete_pending_boundary"
    state = _apply(state, work.events)

    boundary = engine.plan_window(state, plan, _context(1))
    assert boundary.outcome == "advanced"
    assert boundary.metrics.integration_boundary == "production_quota_exhausted"
    state = _apply(state, boundary.events)

    completed = engine.plan_window(state, plan, _context(2))
    assert completed.outcome == "completed"
    assert completed.metrics.integration_boundary == "work_purchase_target_affordable"
    assert completed.events == ()
