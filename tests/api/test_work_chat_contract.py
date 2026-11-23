from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from world_engine.adapters.models import RoleTask
from world_engine.api.app import create_app
from world_engine.api.contracts import MessageRequest
from world_engine.api.security import LocalSession
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.study_chat import MAX_SAFE_INTEGER
from world_engine.application.work_chat import (
    FollowWorkRoutineIntent,
    QueueWorkIntent,
    WorkPurchaseTargetIntent,
    SavedWorkRoutineError,
    _explicitly_names_assignment,
    build_saved_work_routine_plan,
    build_work_chat_plan,
    work_message_key,
    work_queue_reply,
)
from world_engine.domain.components import (
    DailyRoutineComponent, DailyRoutineWindow, InventoryComponent, InventoryLot, LifeComponent, MarketComponent,
    MarketOffer, ProductionQuotaPolicy, ScheduledEventRecord,
)
from world_engine.domain.models import AccessScope, CalendarConfig, JobRecord, JobStatus, NewWorld, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.runtime import build_application, local_identity
from world_engine.application.worlds import WorldApplication
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.simulation import LongActionPlan
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


DAY = 86_400
ACTOR_ID = UUID("10000000-0000-4000-8000-000000000001")
EMPLOYER_ID = UUID("20000000-0000-4000-8000-000000000002")
WORKPLACE_ID = UUID("30000000-0000-4000-8000-000000000003")
POSTING_ID = UUID("40000000-0000-4000-8000-000000000004")
SOURCE_ID = UUID("50000000-0000-4000-8000-000000000005")
ORIGIN_ID = UUID("51000000-0000-4000-8000-000000000005")
WORKER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
EMPLOYER_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")
SELLER_ID = UUID("71000000-0000-4000-8000-000000000007")
STOCK_ACCOUNT_ID = UUID("72000000-0000-4000-8000-000000000007")
STOCK_LOT_ID = UUID("73000000-0000-4000-8000-000000000007")
LISTING_ID = UUID("74000000-0000-4000-8000-000000000007")
ITEM_RESOURCE_ID = "cultivation.herb"


def _world(*, day_seconds=DAY, shift_seconds=3_600, tick=0, posting_status="open", valid_until=None):
    source = OccupationSource(kind="event", reference_id=str(SOURCE_ID))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, ACTOR_ID, POSTING_ID),
        actor_id=ACTOR_ID,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Registry clerk",
        started_tick=0,
        revision=1,
        source=source,
    )
    posting = WorkPosting(
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Registry clerk",
        slots=1,
        valid_from_tick=0,
        valid_until_tick=valid_until,
        status=posting_status,
    )
    terms = WorkShiftTerms(
        posting_id=POSTING_ID,
        duration_seconds=shift_seconds,
        resource_id="core.silver",
        wage="12.50",
    )
    state = WorldState(
        current_tick=tick,
        player_id=ACTOR_ID,
        calendar=CalendarConfig(day_seconds=day_seconds, year_days=360),
        entities={
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                status="active",
                location_id=WORKPLACE_ID,
            ),
            EMPLOYER_ID: WorldEntity(
                id=EMPLOYER_ID,
                kind="organization",
                status="active",
                components={"treasury": {"accounts": {"core.silver": str(EMPLOYER_ACCOUNT_ID)}}},
            ),
            WORKPLACE_ID: WorldEntity(id=WORKPLACE_ID, kind="place", status="active"),
        },
        resources={
            WORKER_ACCOUNT_ID: ResourceAccount(
                resource_id="core.silver", balance="5", owner_id=ACTOR_ID,
            ),
            EMPLOYER_ACCOUNT_ID: ResourceAccount(
                resource_id="core.silver", balance="100", owner_id=EMPLOYER_ID,
            ),
        },
        work_postings=[posting],
        work_assignments=[assignment],
        metadata={"work_shift_terms": [terms.model_dump(mode="json")]},
    )
    return state, assignment


def _action(assignment_id, *, amount=1, unit="day", first_shift_tick=0,
            repeat_every_days=1, balance_target=None, purchase_target=None):
    return QueueWorkIntent(
        kind="queue_work",
        assignment_id=assignment_id,
        first_shift_tick=first_shift_tick,
        repeat_every_days=repeat_every_days,
        amount=amount,
        unit=unit,
        balance_target=balance_target,
        purchase_target=purchase_target,
    )


def _with_work_routine(state, *, windows=None):
    actor = state.entities[ACTOR_ID]
    components = dict(actor.components)
    components["daily_routine"] = DailyRoutineComponent(windows=windows or [DailyRoutineWindow(
        window_id=UUID("81000000-0000-4000-8000-000000000008"),
        start_second=9_000,
        duration_seconds=7_200,
        activity="work",
        place_id=WORKPLACE_ID,
    )]).model_dump(mode="json")
    return WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": state.entities | {ACTOR_ID: actor.model_copy(update={"components": components})},
    })


def _work_routine_api(tmp_path, state, model_outputs):
    database = Database(f"sqlite:///{(tmp_path / 'work-routine.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    created = repository.create_world(scope, NewWorld(title="Saved work routine", state=state))
    operations = OperationsStore(tmp_path / "work-routine-operations.db")

    class Models:
        def __init__(self):
            self.outputs = list(model_outputs)
            self.calls = []

        async def generate(self, _scope, request):
            self.calls.append(request.role_task)
            if request.role_task is RoleTask.GM_ANSWER:
                output = {"answer": "你可以查看本人保存的工作排班。", "references": []}
            else:
                assert request.role_task is RoleTask.INTENT
                output = self.outputs.pop(0)
            Draft202012Validator(request.output_schema).validate(output)
            return SimpleNamespace(normalized_output=output)

    models = Models()
    worlds = WorldApplication(repository, None)
    actions = RulesActionDispatcher(repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
    service = ConversationApplication(worlds, models, ConversationStore(operations), actions=actions)
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    client = TestClient(create_app(worlds, session, conversations=service), base_url="http://testserver")
    client.get("/api/v1/session").raise_for_status()
    return client, database, repository, scope, created, models


def _post_message(client, created, *, request_id, text, mode="action"):
    request = MessageRequest(
        request_id=request_id,
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        session_id=created.branch.branch_id,
        actor_id=ACTOR_ID,
        expected_state_version="0",
        mode=mode,
        text=text,
    )
    return client.post(
        f"/api/v1/worlds/{created.world.world_id}/messages",
        json=request.model_dump(mode="json"),
        headers={"Idempotency-Key": str(request_id)},
    )


def _follow_action(assignment_id=None, *, amount=1, unit="month", balance_target=None):
    action = {"kind": "follow_work_routine", "amount": amount, "unit": unit}
    if assignment_id is not None:
        action["assignment_id"] = str(assignment_id)
    if balance_target is not None:
        action["balance_target"] = balance_target
    return {"action": action, "clarification": ""}


def _with_other_actor_assignment(state, *, actor_id):
    posting_id = UUID("82000000-0000-4000-8000-000000000008")
    source = OccupationSource(kind="event", reference_id="82000000-0000-4000-8000-000000000009")
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, actor_id, posting_id),
        actor_id=actor_id,
        posting_id=posting_id,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Archive assistant",
        started_tick=0,
        revision=1,
        source=source,
    )
    posting = WorkPosting(
        posting_id=posting_id,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Archive assistant",
        slots=1,
        valid_from_tick=0,
        status="open",
    )
    terms = WorkShiftTerms(
        posting_id=posting_id,
        duration_seconds=3_600,
        resource_id="core.silver",
        wage="12.50",
    )
    entities = dict(state.entities)
    entities[actor_id] = WorldEntity(
        id=actor_id, kind="character", status="active", location_id=WORKPLACE_ID,
    )
    return WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": entities,
        "work_postings": [*state.work_postings, posting],
        "work_assignments": [*state.work_assignments, assignment],
        "metadata": state.metadata | {
            "work_shift_terms": [
                *state.metadata["work_shift_terms"], terms.model_dump(mode="json"),
            ],
        },
    }), assignment


def _world_with_market(*, currency_resource_id="core.silver", listing_id=LISTING_ID, active=True):
    state, assignment = _world()
    workplace = state.entities[WORKPLACE_ID].model_copy(update={
        "components": {
            "market": MarketComponent(
                currency_resource_id=currency_resource_id,
                offers=[MarketOffer(
                    listing_id=listing_id,
                    resource_id=ITEM_RESOURCE_ID,
                    stock_account_id=STOCK_ACCOUNT_ID,
                    seller_id=SELLER_ID,
                    unit_price_minor=125,
                    minimum_quantity="2",
                    active=active,
                )],
            ).model_dump(mode="json"),
        },
    })
    seller = WorldEntity(
        id=SELLER_ID,
        kind="organization",
        status="active",
        location_id=WORKPLACE_ID,
        components={
            "inventory": InventoryComponent(lots=[InventoryLot(
                lot_id=STOCK_LOT_ID,
                account_id=STOCK_ACCOUNT_ID,
                resource_id=ITEM_RESOURCE_ID,
                quantity="100",
            )]).model_dump(mode="json"),
        },
    )
    return WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": state.entities | {WORKPLACE_ID: workplace, SELLER_ID: seller},
        "resources": state.resources | {
            STOCK_ACCOUNT_ID: ResourceAccount(
                resource_id=ITEM_RESOURCE_ID, balance="100", owner_id=SELLER_ID,
            ).model_dump(mode="python"),
        },
    }), assignment


def test_queue_work_intent_is_closed_strict_and_exact():
    valid, assignment = _world()
    action = _action(assignment.assignment_id, amount=1, unit="month", balance_target="125.50")
    assert action.unit == "month"
    assert action.first_shift_tick == 0
    assert action.balance_target == Decimal("125.50")
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=True, repeat_every_days=1, amount=1, unit="day",
        )
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=2**63, repeat_every_days=1, amount=1, unit="day",
        )
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=0, repeat_every_days=0, amount=1, unit="day",
        )
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=0, repeat_every_days=1, amount=True, unit="day",
        )
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=0, repeat_every_days=1, amount=MAX_SAFE_INTEGER + 1, unit="day",
        )
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=0, repeat_every_days=1, amount=1, unit="decade",
        )
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=0, repeat_every_days=1, amount=1, unit="day",
            balance_target="-0.01",
        )
    with pytest.raises(ValidationError):
        QueueWorkIntent(
            kind="queue_work", assignment_id=assignment.assignment_id,
            first_shift_tick=0, repeat_every_days=1, amount=1, unit="day",
            route_id=uuid4(),
        )
    target = WorkPurchaseTargetIntent(listing_id=LISTING_ID, quantity="2.5")
    assert target.quantity == Decimal("2.5")
    with pytest.raises(ValidationError):
        WorkPurchaseTargetIntent(listing_id=LISTING_ID, quantity="0")
    with pytest.raises(ValidationError):
        _action(
            assignment.assignment_id,
            balance_target="100",
            purchase_target={"listing_id": str(LISTING_ID), "quantity": "2"},
        )


def test_follow_work_routine_intent_omits_guessed_shift_fields_and_reuses_exact_daily_phase():
    state, assignment = _world()
    state = _with_work_routine(state)
    action = FollowWorkRoutineIntent(
        kind="follow_work_routine", amount=1, unit="month", balance_target="65",
    )

    plan = build_saved_work_routine_plan(
        state, ACTOR_ID, action, explicit_text="照常上班一个月。",
    )
    assert plan.started_tick == 0
    assert plan.work_schedule.assignment_id == assignment.assignment_id
    assert plan.work_schedule.first_shift_tick == 12_600
    assert plan.work_schedule.repeat_every_days == 1
    assert plan.work_schedule.balance_target == Decimal("65")
    assert plan.target_tick == 30 * DAY

    shift_end, _ = _world(tick=16_200)
    next_day_plan = build_saved_work_routine_plan(
        _with_work_routine(shift_end),
        ACTOR_ID,
        action,
        explicit_text="照常上班一个月。",
    )
    assert next_day_plan.work_schedule.first_shift_tick == 12_600 + DAY

    with pytest.raises(SavedWorkRoutineError, match="active_work_job") as raised:
        queued = JobRecord(
            job_id=UUID("24000000-0000-4000-8000-000000000024"),
            tenant_id=UUID("21000000-0000-4000-8000-000000000021"),
            world_id=UUID("25000000-0000-4000-8000-000000000025"),
            branch_id=UUID("26000000-0000-4000-8000-000000000026"),
            job_type="simulation.long_action",
            status=JobStatus.QUEUED,
            payload={"plan": plan.model_dump(mode="json")},
            attempt_count=0,
            fencing_token=0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        build_saved_work_routine_plan(
            state, ACTOR_ID, action, explicit_text="照常上班一个月。", existing_jobs=[queued],
        )
    assert raised.value.code == "active_work_job"


def test_explicit_assignment_selection_fails_closed_when_text_names_two_jobs():
    state, first = _world()
    state = _with_work_routine(state)
    other_actor_id = UUID("84000000-0000-4000-8000-000000000008")
    state, second = _with_other_actor_assignment(state, actor_id=other_actor_id)
    second = second.model_copy(update={"actor_id": ACTOR_ID})
    eligible = [(first, None, None), (second, None, None)]

    assert not _explicitly_names_assignment(
        state,
        "照常上班，Registry clerk 或 Archive assistant 都可以，一个月。",
        first.assignment_id,
        eligible,
    )
    assert _explicitly_names_assignment(
        state,
        "照常上班，按 Archive assistant 的班表上一个月。",
        second.assignment_id,
        eligible,
    )


def test_build_plan_converts_month_to_30_world_days_and_does_not_mutate_state():
    state, assignment = _world()
    before = state.model_dump(mode="python")
    action = _action(
        assignment.assignment_id,
        amount=1,
        unit="month",
        first_shift_tick=5_000,
        repeat_every_days=1,
        balance_target="80",
    )

    plan = build_work_chat_plan(state, ACTOR_ID, action)

    assert plan.activity == "work"
    assert plan.started_tick == 0
    assert plan.target_tick == 30 * DAY
    assert plan.work_schedule.assignment_id == assignment.assignment_id
    assert plan.work_schedule.first_shift_tick == 5_000
    assert plan.work_schedule.repeat_every_days == 1
    assert plan.work_schedule.balance_target == Decimal("80")
    assert state.model_dump(mode="python") == before


def test_purchase_target_is_resolved_from_trusted_local_market_and_visible_allowlist():
    state, assignment = _world_with_market()
    before = state.model_dump(mode="python")
    action = _action(
        assignment.assignment_id,
        amount=1,
        unit="month",
        purchase_target={"listing_id": str(LISTING_ID), "quantity": "4"},
    )

    plan = build_work_chat_plan(
        state,
        ACTOR_ID,
        action,
        visible_market_offers=[{
            "listing_id": str(LISTING_ID),
            "resource_id": "player.supplied.fake_item",
            "currency_resource_id": "player.supplied.fake_currency",
            "seller_id": str(EMPLOYER_ID),
        }],
    )

    target = plan.work_schedule.purchase_target
    assert plan.work_schedule.balance_target is None
    assert target.market_id == WORKPLACE_ID
    assert target.listing_id == LISTING_ID
    assert target.resource_id == ITEM_RESOURCE_ID
    assert target.currency_resource_id == "core.silver"
    assert target.seller_id == SELLER_ID
    assert target.quantity == Decimal("4")
    assert state.model_dump(mode="python") == before


def test_purchase_target_rejects_foreign_unseen_wrong_currency_and_missing_listings():
    state, assignment = _world_with_market()
    action = _action(
        assignment.assignment_id,
        purchase_target={"listing_id": str(LISTING_ID), "quantity": "2"},
    )

    with pytest.raises(ValueError, match="visible local market offers"):
        build_work_chat_plan(state, ACTOR_ID, action)
    with pytest.raises(ValueError, match="visible local market offers"):
        build_work_chat_plan(
            state, ACTOR_ID, action,
            visible_market_offers=[{"listing_id": str(uuid4())}],
        )

    foreign_place = ORIGIN_ID
    foreign_listing_id = LISTING_ID
    entities = dict(state.entities)
    entities[foreign_place] = WorldEntity(id=foreign_place, kind="place", status="active")
    foreign_offer = MarketOffer(
        listing_id=foreign_listing_id,
        resource_id=ITEM_RESOURCE_ID,
        stock_account_id=STOCK_ACCOUNT_ID,
        seller_id=SELLER_ID,
        unit_price_minor=125,
        minimum_quantity="2",
    )
    workplace = entities[WORKPLACE_ID].model_copy(update={
        "components": {"market": MarketComponent(currency_resource_id="core.silver").model_dump(mode="json")},
    })
    foreign_market = entities[foreign_place].model_copy(update={
        "components": {"market": MarketComponent(
            currency_resource_id="core.silver", offers=[foreign_offer],
        ).model_dump(mode="json")},
    })
    seller = entities[SELLER_ID].model_copy(update={"location_id": foreign_place})
    entities.update({WORKPLACE_ID: workplace, foreign_place: foreign_market, SELLER_ID: seller})
    foreign_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": entities,
    })
    with pytest.raises(ValueError, match="active offer at the assigned workplace"):
        build_work_chat_plan(
            foreign_state, ACTOR_ID, action,
            visible_market_offers=[{"listing_id": str(foreign_listing_id)}],
        )

    mismatched_currency, assignment = _world_with_market(currency_resource_id="core.gold")
    mismatched_action = _action(
        assignment.assignment_id,
        purchase_target={"listing_id": str(LISTING_ID), "quantity": "2"},
    )
    with pytest.raises(ValueError, match="wage currency does not match"):
        build_work_chat_plan(
            mismatched_currency, ACTOR_ID, mismatched_action,
            visible_market_offers=[{"listing_id": str(LISTING_ID)}],
        )

    missing_market, assignment = _world_with_market()
    workplace = missing_market.entities[WORKPLACE_ID].model_copy(update={
        "components": {"market": MarketComponent(currency_resource_id="core.silver").model_dump(mode="json")},
    })
    missing_market = WorldState.model_validate({
        **missing_market.model_dump(mode="python"),
        "entities": missing_market.entities | {WORKPLACE_ID: workplace},
    })
    missing_action = _action(
        assignment.assignment_id,
        purchase_target={"listing_id": str(LISTING_ID), "quantity": "2"},
    )
    with pytest.raises(ValueError, match="active offer at the assigned workplace"):
        build_work_chat_plan(
            missing_market, ACTOR_ID, missing_action,
            visible_market_offers=[{"listing_id": str(LISTING_ID)}],
        )


@pytest.mark.parametrize(("amount", "unit", "expected_seconds"), [
    (3_600, "second", 3_600),
    (1, "hour", 3_600),
    (1, "day", DAY),
    (1, "month", 30 * DAY),
    (1, "year", 360 * DAY),
])
def test_work_duration_units_use_calendar_and_30_day_month(amount, unit, expected_seconds):
    state, assignment = _world()

    plan = build_work_chat_plan(
        state, ACTOR_ID,
        _action(assignment.assignment_id, amount=amount, unit=unit, first_shift_tick=0),
    )

    assert plan.target_tick - plan.started_tick == expected_seconds


def test_build_plan_requires_owned_active_assignment_and_current_posting_terms():
    state, assignment = _world()
    other_actor_id = UUID("80000000-0000-4000-8000-000000000008")
    other_actor = WorldEntity(
        id=other_actor_id, kind="character", status="active", location_id=WORKPLACE_ID,
    )
    state = state.model_copy(update={"entities": state.entities | {other_actor_id: other_actor}})
    with pytest.raises(ValueError, match="active assignment held by this character"):
        build_work_chat_plan(state, other_actor_id, _action(assignment.assignment_id))
    with pytest.raises(ValueError, match="active assignment held by this character"):
        build_work_chat_plan(state, ACTOR_ID, _action(uuid4()))

    closed, active = _world(posting_status="closed")
    with pytest.raises(ValueError, match="open registered posting"):
        build_work_chat_plan(closed, ACTOR_ID, _action(active.assignment_id))

    no_terms, active = _world()
    no_terms = no_terms.model_copy(update={"metadata": {}})
    with pytest.raises(ValueError, match="no registered paid-shift terms"):
        build_work_chat_plan(no_terms, ACTOR_ID, _action(active.assignment_id))

    away, active = _world()
    actor = away.entities[ACTOR_ID].model_copy(update={"location_id": ORIGIN_ID})
    away = away.model_copy(update={"entities": away.entities | {ACTOR_ID: actor}})
    with pytest.raises(ValueError, match="at the assigned workplace"):
        build_work_chat_plan(away, ACTOR_ID, _action(active.assignment_id))


def test_build_plan_checks_first_complete_shift_cadence_and_posting_window():
    state, assignment = _world()
    with pytest.raises(ValueError, match="first complete paid shift"):
        build_work_chat_plan(
            state, ACTOR_ID,
            _action(assignment.assignment_id, amount=1, unit="second", first_shift_tick=0),
        )
    with pytest.raises(ValueError, match="cannot precede the current tick"):
        build_work_chat_plan(
            state.model_copy(update={"current_tick": 1}), ACTOR_ID,
            _action(assignment.assignment_id, amount=1, unit="day", first_shift_tick=0),
        )
    short_day, assignment = _world(day_seconds=1_800)
    with pytest.raises(ValueError, match="repeat interval"):
        build_work_chat_plan(
            short_day, ACTOR_ID,
            _action(assignment.assignment_id, amount=2, unit="hour", repeat_every_days=1),
        )
    dated, assignment = _world(valid_until=3_000)
    with pytest.raises(ValueError, match="posting validity window"):
        build_work_chat_plan(
            dated, ACTOR_ID,
            _action(assignment.assignment_id, amount=1, unit="day", first_shift_tick=0),
        )


def test_build_plan_rejects_unsafe_calendar_products_and_tick_overflow():
    state, assignment = _world()
    with pytest.raises(ValueError, match="month count exceeds the safe integer limit"):
        build_work_chat_plan(
            state, ACTOR_ID,
            _action(assignment.assignment_id, amount=MAX_SAFE_INTEGER, unit="month"),
        )
    near_limit, assignment = _world(tick=2**63 - 100)
    with pytest.raises(ValueError, match="exceeds the supported world clock"):
        build_work_chat_plan(
            near_limit, ACTOR_ID,
            _action(assignment.assignment_id, amount=1, unit="hour", first_shift_tick=2**63 - 100),
        )


def test_work_message_key_and_reply_are_stable_from_persisted_plan():
    state, assignment = _world()
    scope = AccessScope(
        tenant_id=UUID("21000000-0000-4000-8000-000000000021"),
        user_id=UUID("22000000-0000-4000-8000-000000000022"),
    )
    request_id = UUID("23000000-0000-4000-8000-000000000023")
    plan = build_work_chat_plan(
        state, ACTOR_ID,
        _action(
            assignment.assignment_id, amount=1, unit="month", first_shift_tick=0,
            balance_target="65",
        ),
    )
    job = JobRecord(
        job_id=UUID("24000000-0000-4000-8000-000000000024"),
        tenant_id=scope.tenant_id,
        world_id=UUID("25000000-0000-4000-8000-000000000025"),
        branch_id=UUID("26000000-0000-4000-8000-000000000026"),
        job_type="simulation.long_action",
        status=JobStatus.QUEUED,
        payload={"plan": plan.model_dump(mode="json")},
        attempt_count=0,
        fencing_token=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    reply = work_queue_reply(request_id, job)
    running = job.model_copy(update={"status": JobStatus.RUNNING, "committed_through_tick": 100_000})

    assert work_message_key(scope, request_id) == f"work-message:{scope.user_id}:{request_id}"
    assert reply == work_queue_reply(request_id, running)
    assert reply["job_id"] == str(job.job_id)
    assert reply["elapsed_ticks"] == "0"
    assert reply["world_effect"] is False
    assert "2,592,000" in reply["content"]
    assert "30 个世界日" in reply["content"]
    assert "达到本人该岗位工资账户余额 65 时提前结束" in reply["content"]
    assert reply["references"] == [str(assignment.assignment_id)]

    no_target = build_work_chat_plan(
        state, ACTOR_ID, _action(assignment.assignment_id, amount=1, unit="month"),
    )
    without_goal = job.model_copy(update={"payload": {"plan": no_target.model_dump(mode="json")}})
    assert "工资账户余额" not in work_queue_reply(request_id, without_goal)["content"]


def test_purchase_queue_reply_mentions_tracking_not_purchase_and_adds_its_reference():
    state, assignment = _world_with_market()
    plan = build_work_chat_plan(
        state,
        ACTOR_ID,
        _action(
            assignment.assignment_id,
            amount=1,
            unit="month",
            purchase_target={"listing_id": str(LISTING_ID), "quantity": "3"},
        ),
        visible_market_offers=[{"listing_id": str(LISTING_ID)}],
    )
    request_id = UUID("23000000-0000-4000-8000-000000000023")
    job = JobRecord(
        job_id=UUID("24000000-0000-4000-8000-000000000024"),
        tenant_id=UUID("21000000-0000-4000-8000-000000000021"),
        world_id=UUID("25000000-0000-4000-8000-000000000025"),
        branch_id=UUID("26000000-0000-4000-8000-000000000026"),
        job_type="simulation.long_action",
        status=JobStatus.QUEUED,
        payload={"plan": plan.model_dump(mode="json")},
        attempt_count=0,
        fencing_token=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    reply = work_queue_reply(request_id, job)

    assert "当前市场报价" in reply["content"]
    assert "不会替你购买" in reply["content"]
    assert reply["references"] == [str(assignment.assignment_id), str(LISTING_ID)]


def test_long_action_purchase_target_api_is_idempotent_and_rejects_caller_trusted_fields(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        state, assignment = _world_with_market()
        created = repo.create_world(scope, NewWorld(title="Work purchase target", state=state))
        path = f"/api/v1/worlds/{created.world.world_id}/long-actions"
        headers = {"Idempotency-Key": "work-purchase-target"}

        def body(quantity: str, listing_id: UUID = LISTING_ID) -> dict[str, object]:
            return {
                "branch_id": str(created.branch.branch_id),
                "expected_state_version": "0",
                "duration_seconds": DAY,
                "activity": "work",
                "work_schedule": {
                    "assignment_id": str(assignment.assignment_id),
                    "first_shift_tick": 0,
                    "repeat_every_days": 1,
                    "purchase_target": {"listing_id": str(listing_id), "quantity": quantity},
                },
            }

        accepted = client.post(path, json=body("3"), headers=headers)
        assert accepted.status_code == 202
        job_id = UUID(accepted.json()["job_id"])
        persisted = LongActionPlan.model_validate(repo.read_job(scope, job_id).payload["plan"])
        target = persisted.work_schedule.purchase_target
        assert target is not None
        assert target.listing_id == LISTING_ID
        assert target.market_id == WORKPLACE_ID
        assert target.resource_id == ITEM_RESOURCE_ID
        assert target.currency_resource_id == "core.silver"
        assert target.seller_id == SELLER_ID
        assert target.quantity == Decimal("3")

        retry = client.post(path, json=body("3"), headers=headers)
        assert retry.status_code == 202
        assert retry.json()["job_id"] == str(job_id)
        changed_quantity = client.post(path, json=body("4"), headers=headers)
        assert changed_quantity.status_code == 409

        unknown = client.post(
            path,
            json=body("3", listing_id=uuid4()),
            headers={"Idempotency-Key": "work-purchase-unknown"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "ROUTINE_WORK_PURCHASE_TARGET_UNAVAILABLE"

        with_trusted_fields = body("3")
        with_trusted_fields["work_schedule"]["purchase_target"].update({
            "market_id": str(WORKPLACE_ID),
            "resource_id": ITEM_RESOURCE_ID,
            "currency_resource_id": "core.silver",
            "seller_id": str(SELLER_ID),
        })
        forged = client.post(
            path,
            json=with_trusted_fields,
            headers={"Idempotency-Key": "work-purchase-forged"},
        )
        assert forged.status_code == 422


def test_saved_work_routine_message_reuses_unique_schedule_and_does_not_advance_time(tmp_path):
    state, assignment = _world()
    state = _with_work_routine(state)
    client, database, repository, scope, created, models = _work_routine_api(
        tmp_path, state, [_follow_action(balance_target="65"), _follow_action(balance_target="65")],
    )
    try:
        request_id = uuid4()
        first = _post_message(client, created, request_id=request_id, text="照常上班一个月，攒到 65 银币就停。")
        assert first.status_code == 200
        result = first.json()
        assert result["kind"] == "action"
        assert result["world_effect"] is False
        assert result["elapsed_ticks"] == "0"
        assert "首班世界时刻为 12600" in result["content"]
        assert "每 1 个世界日重复一次" in result["content"]
        assert "工资账户余额 65" in result["content"]

        jobs = repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)
        job, = jobs
        plan = LongActionPlan.model_validate(job.payload["plan"])
        assert job.job_type == "simulation.long_action" and job.status is JobStatus.QUEUED
        assert plan.actor_id == ACTOR_ID
        assert plan.work_schedule.assignment_id == assignment.assignment_id
        assert plan.work_schedule.first_shift_tick == 12_600
        assert plan.work_schedule.repeat_every_days == 1
        assert plan.work_schedule.balance_target == Decimal("65")

        retry = _post_message(client, created, request_id=request_id, text="照常上班一个月，攒到 65 银币就停。")
        assert retry.status_code == 200 and retry.json() == result
        assert len(repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)) == 1

        overlap = _post_message(client, created, request_id=uuid4(), text="照常上班一个月。")
        assert overlap.status_code == 200
        assert overlap.json()["kind"] == "clarification"
        assert "已有持续工作作业" in overlap.json()["content"]
        assert len(repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)) == 1
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert after.state.current_tick == 0 and after.last_seq == 0
        assert models.calls == [RoleTask.INTENT, RoleTask.INTENT]
    finally:
        client.close()
        database.dispose()


def test_a_month_of_work_replays_equally_from_chat_and_long_action_panel(tmp_path):
    state, assignment = _world_with_market()
    state = _with_work_routine(state)
    first_shift = 12_600
    shift_length = 3_600
    deadline = 30 * DAY
    neighbor_id, quota_source_id = uuid4(), uuid4()
    raw = state.model_dump(mode="json")
    raw["resources"][str(EMPLOYER_ACCOUNT_ID)]["balance"] = "500"
    raw["entities"][str(neighbor_id)] = WorldEntity(
        id=neighbor_id, kind="person", status="active", location_id=WORKPLACE_ID,
        components={"life": LifeComponent(birth_tick=0, lifespan_ticks=first_shift + shift_length).model_dump(mode="json")},
    ).model_dump(mode="json")
    raw["entities"][str(WORKPLACE_ID)]["components"]["production"] = {"quotas": [ProductionQuotaPolicy(
        quota_id="quota.monthly", resource_id="core.grain", source_account_id=quota_source_id,
        period_ticks=deadline, per_period="1", maximum_renewal="1", base_credit_limit="1",
        issued_total="1", last_issued_period=0,
    ).model_dump(mode="json")]}
    raw["resources"][str(quota_source_id)] = ResourceAccount(
        resource_id="core.grain", balance="0", owner_id=WORKPLACE_ID,
        account_type="production_source", credit_limit="2",
    ).model_dump(mode="json")
    raw["scheduled_events"] = [ScheduledEventRecord(
        id=uuid4(), due_tick=deadline, phase="environment", handler_id="production.quota_renewal",
        handler_version="1.0.0", payload={"producer_id": str(WORKPLACE_ID), "quota_id": "quota.monthly"},
    ).model_dump(mode="json")]
    state = WorldState.model_validate(raw)
    client, database, repository, scope, chat_world, models = _work_routine_api(tmp_path, state, [_follow_action()])
    with client:
        try:
            panel_world = repository.create_world(scope, NewWorld(title="Panel work routine", state=state))
            message_id = uuid4()
            chat = _post_message(client, chat_world, request_id=message_id, text="照常上班一个月攒钱。")
            chat.raise_for_status()
            assert chat.json()["world_effect"] is False
            panel_body = {
                "branch_id": str(panel_world.branch.branch_id), "expected_state_version": "0",
                "duration_seconds": deadline, "activity": "work", "work_schedule": {
                    "assignment_id": str(assignment.assignment_id), "first_shift_tick": first_shift,
                    "repeat_every_days": 1,
                },
            }
            panel_path = f"/api/v1/worlds/{panel_world.world.world_id}/long-actions"
            headers = {"Idempotency-Key": "month-of-work"}
            panel = client.post(panel_path, json=panel_body, headers=headers)
            panel.raise_for_status()
            chat_id, panel_id = UUID(chat.json()["job_id"]), UUID(panel.json()["job_id"])
            assert repository.read_job(scope, chat_id).payload == repository.read_job(scope, panel_id).payload

            for created, job_id in [(chat_world, chat_id), (panel_world, panel_id)]:
                world_id, branch_id = created.world.world_id, created.branch.branch_id
                assert repository.read_world(scope, world_id, branch_id).state.current_tick == 0
                jobs = SimulationJobs(repository)
                lease = repository.claim_job("month-work-integration")
                assert lease is not None and lease.job_id == job_id
                resumed = False
                for _ in range(100):
                    jobs.advance(scope, lease)
                    job = repository.read_job(scope, job_id)
                    if job.status is JobStatus.SUCCEEDED:
                        break
                    assert job.status is JobStatus.RUNNING, job.result
                    if not resumed and job.payload.get("work_progress", {}).get("shifts_paid") == 1:
                        paused = repository.control_job(scope, job_id, "pause")
                        assert paused.status is JobStatus.PAUSED
                        assert paused.committed_through_tick == first_shift + shift_length
                        repository.control_job(scope, job_id, "resume")
                        lease = repository.claim_job("month-work-resumed")
                        assert lease is not None and lease.job_id == job_id
                        jobs = SimulationJobs(repository)
                        resumed = True
                else:
                    pytest.fail("Month-long work did not finish within the expected number of windows")

                final = repository.read_world(scope, world_id, branch_id)
                assert resumed and job.payload["work_progress"]["shifts_paid"] == 30
                assert final.state.current_tick == job.committed_through_tick == deadline
                assert final.state.resources[WORKER_ACCOUNT_ID].balance == Decimal("380")
                assert final.state.resources[EMPLOYER_ACCOUNT_ID].balance == Decimal("125")
                assert final.state.resources[STOCK_ACCOUNT_ID].balance == Decimal("100")
                assert final.state.entities[neighbor_id].status == "dead"
                assert final.state.scheduled_events[0].status == "completed"
                events = repository.list_events(scope, world_id, branch_id, limit=250).events
                assert [event.tick for event in events if event.type == "core.work_shift_paid.v1"] == [
                    first_shift + shift_length + day * DAY for day in range(30)
                ]
                assert [event.tick for event in events if event.type == "core.scheduled_event_completed.v1"] == [deadline]
                assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash

            assert _post_message(client, chat_world, request_id=message_id, text="照常上班一个月攒钱。").json() == chat.json()
            retry = client.post(panel_path, json=panel_body, headers=headers)
            retry.raise_for_status()
            assert retry.json()["job_id"] == str(panel_id)
            assert models.calls == [RoleTask.INTENT]
        finally:
            database.dispose()


def test_chat_and_long_action_panel_reject_work_from_away_without_a_savings_target(tmp_path):
    state, assignment = _world()
    raw = state.model_dump(mode="json")
    raw["entities"][str(ORIGIN_ID)] = WorldEntity(id=ORIGIN_ID, kind="place").model_dump(mode="json")
    raw["entities"][str(ACTOR_ID)]["location_id"] = str(ORIGIN_ID)
    state = WorldState.model_validate(raw)
    proposed = {"action": _action(assignment.assignment_id).model_dump(mode="json"), "clarification": ""}
    client, database, repository, scope, created, _ = _work_routine_api(tmp_path, state, [proposed])
    try:
        with client:
            chat = _post_message(client, created, request_id=uuid4(), text="从现在起每天去这个岗位上一班，做一天。")
            chat.raise_for_status()
            assert chat.json()["kind"] == "clarification"
            panel = client.post(f"/api/v1/worlds/{created.world.world_id}/long-actions", json={
                "branch_id": str(created.branch.branch_id), "expected_state_version": "0",
                "duration_seconds": DAY, "activity": "work", "work_schedule": {
                    "assignment_id": str(assignment.assignment_id), "first_shift_tick": 0, "repeat_every_days": 1,
                },
            }, headers={"Idempotency-Key": "work-from-away"})
            assert panel.status_code == 422
            assert panel.json()["error"]["code"] == "ROUTINE_WORK_ASSIGNMENT_UNAVAILABLE"
            assert not repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)
    finally:
        database.dispose()


@pytest.mark.parametrize("case", [
    "missing_assignment",
    "missing_routine",
    "wrong_workplace",
    "ambiguous_work_window",
    "other_actor_assignment",
])
def test_saved_work_routine_api_clarifies_without_creating_job(tmp_path, case):
    state, assignment = _world()
    output = _follow_action()
    expected_text = {
        "missing_assignment": "没有找到你本人当前有效的任职",
        "missing_routine": "没有可沿用的已保存日常排班",
        "wrong_workplace": "请先到你本人任职的工作地点",
        "ambiguous_work_window": "多个匹配的工作时段",
        "other_actor_assignment": "不是本人当前有效且可排班的任职",
    }[case]
    if case == "missing_assignment":
        state = WorldState.model_validate({
            **state.model_dump(mode="python"), "work_assignments": [],
        })
        state = _with_work_routine(state)
    elif case == "missing_routine":
        pass
    elif case == "wrong_workplace":
        state = _with_work_routine(state)
        actor = state.entities[ACTOR_ID].model_copy(update={"location_id": ORIGIN_ID})
        state = WorldState.model_validate({
            **state.model_dump(mode="python"),
            "entities": state.entities | {
                ORIGIN_ID: WorldEntity(id=ORIGIN_ID, kind="place", status="active"),
                ACTOR_ID: actor,
            },
        })
    elif case == "ambiguous_work_window":
        state = _with_work_routine(state, windows=[
            DailyRoutineWindow(
                window_id=UUID("81000000-0000-4000-8000-000000000008"),
                start_second=9_000, duration_seconds=7_200, activity="work", place_id=WORKPLACE_ID,
            ),
            DailyRoutineWindow(
                window_id=UUID("83000000-0000-4000-8000-000000000008"),
                start_second=30_000, duration_seconds=3_600, activity="work", place_id=WORKPLACE_ID,
            ),
        ])
    else:
        other_actor_id = UUID("84000000-0000-4000-8000-000000000008")
        state = _with_work_routine(state)
        state, foreign_assignment = _with_other_actor_assignment(state, actor_id=other_actor_id)
        output = _follow_action(foreign_assignment.assignment_id)

    client, database, repository, scope, created, _models = _work_routine_api(tmp_path, state, [output])
    try:
        response = _post_message(client, created, request_id=uuid4(), text="照常上班一个月。")
        assert response.status_code == 200
        assert response.json()["kind"] == "clarification"
        assert expected_text in response.json()["content"]
        assert repository.list_jobs(scope, created.world.world_id, created.branch.branch_id) == []
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert after.state.current_tick == 0 and after.last_seq == 0
    finally:
        client.close()
        database.dispose()


def test_saved_work_routine_question_stays_a_query(tmp_path):
    state, _assignment = _world()
    state = _with_work_routine(state)
    client, database, repository, scope, created, models = _work_routine_api(tmp_path, state, [])
    try:
        response = _post_message(
            client, created, request_id=uuid4(), text="我平常几点上班？", mode="ask_gm",
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "query"
        assert repository.list_jobs(scope, created.world.world_id, created.branch.branch_id) == []
        assert models.calls == [RoleTask.GM_ANSWER]
    finally:
        client.close()
        database.dispose()
