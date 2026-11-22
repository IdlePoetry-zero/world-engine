"""Repository proof for resuming a named work purchase target across revisions."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.application.authoring import AuthoringApplication
from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import InventoryComponent, InventoryLot, MarketComponent, MarketOffer, TreasuryComponent
from world_engine.domain.models import (
    AccessScope,
    JobStatus,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.domain.occupation_events import OccupationSource
from world_engine.domain.occupations import WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.simulation.contracts import LongActionPlan, RoutineWorkSchedule, WorkPurchaseTarget
from world_engine.simulation.work_purchase import observe_work_purchase_quote
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


DAY = 86_400
RESOURCE_ID = "core.silver"
GOODS_ID = "core.grain"
HIGH_PRICE = 100
LOW_PRICE = 5
JUST_AFFORDABLE_PRICE = 7
QUANTITY = Decimal("3")
WAGE = Decimal("20.00")
INITIAL_WALLET = Decimal("2.25")
INITIAL_STOCK = Decimal("5")


def _state(*, price=HIGH_PRICE):
    worker_id, employer_id, workplace_id, worker_account_id, employer_account_id, seller_id, stock_account_id, posting_id = (
        uuid4() for _ in range(8)
    )
    listing_id, lot_id = uuid4(), uuid4()
    source = OccupationSource(kind="genesis", reference_id="fixture.work-purchase-employment")
    assignment_id = occupation_assignment_id(source, worker_id, posting_id)
    market = MarketComponent(
        currency_resource_id=RESOURCE_ID,
        offers=[MarketOffer(
            listing_id=listing_id,
            resource_id=GOODS_ID,
            stock_account_id=stock_account_id,
            seller_id=seller_id,
            unit_price_minor=price,
            minimum_quantity="2",
        )],
    )
    state = WorldState(
        current_tick=0,
        calendar={"day_seconds": DAY},
        player_id=worker_id,
        entities={
            worker_id: WorldEntity(
                id=worker_id, kind="character", name="Mira", location_id=workplace_id,
            ),
            employer_id: WorldEntity(
                id=employer_id, kind="organization", name="Canal Survey Office", location_id=workplace_id,
                components={"treasury": TreasuryComponent(
                    accounts={RESOURCE_ID: employer_account_id},
                ).model_dump(mode="json")},
            ),
            workplace_id: WorldEntity(
                id=workplace_id, kind="place", name="Survey Office",
                components={"market": market.model_dump(mode="json")},
            ),
            seller_id: WorldEntity(
                id=seller_id, kind="organization", name="Grain Seller", location_id=workplace_id,
                components={"inventory": InventoryComponent(lots=[InventoryLot(
                    lot_id=lot_id, account_id=stock_account_id, resource_id=GOODS_ID,
                    quantity=INITIAL_STOCK,
                )]).model_dump(mode="json")},
            ),
        },
        resources={
            worker_account_id: ResourceAccount(
                resource_id=RESOURCE_ID, balance=INITIAL_WALLET, owner_id=worker_id,
            ),
            employer_account_id: ResourceAccount(
                resource_id=RESOURCE_ID, balance="100.00", owner_id=employer_id,
            ),
            stock_account_id: ResourceAccount(
                resource_id=GOODS_ID, balance=INITIAL_STOCK, owner_id=seller_id,
            ),
        },
        work_postings=[WorkPosting(
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=workplace_id,
            title="Surveyor",
            slots=1,
            valid_from_tick=0,
        )],
        work_assignments=[WorkAssignment(
            assignment_id=assignment_id,
            actor_id=worker_id,
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=workplace_id,
            title="Surveyor",
            started_tick=0,
            revision=1,
            source=source,
        )],
        metadata={"work_shift_terms": [WorkShiftTerms(
            posting_id=posting_id,
            duration_seconds=DAY,
            resource_id=RESOURCE_ID,
            wage=WAGE,
        ).model_dump(mode="json")]},
    )
    target = WorkPurchaseTarget(
        market_id=workplace_id,
        listing_id=listing_id,
        resource_id=GOODS_ID,
        currency_resource_id=RESOURCE_ID,
        seller_id=seller_id,
        quantity=QUANTITY,
    )
    plan = LongActionPlan(
        actor_id=worker_id,
        started_tick=0,
        target_tick=DAY * 3,
        activity="work",
        work_schedule=RoutineWorkSchedule(
            assignment_id=assignment_id,
            first_shift_tick=0,
            repeat_every_days=1,
            purchase_target=target,
        ),
    )
    return state, plan, worker_account_id, employer_account_id, stock_account_id, seller_id, workplace_id, listing_id


def _revise_listing(authoring, repository, scope, world_id, branch_id, workplace_id, *, active, price, reason):
    view = repository.read_world(scope, world_id, branch_id)
    market = MarketComponent.model_validate(view.state.entities[workplace_id].components["market"])
    offers = [
        offer.model_copy(update={"active": active, "unit_price_minor": price})
        for offer in market.offers
    ]
    preview = authoring.preview(
        scope,
        world_id,
        branch_id,
        workplace_id,
        "market",
        market.model_copy(update={"offers": offers}).model_dump(mode="json"),
        reason,
    )
    first = authoring.commit(scope, world_id, UUID(preview["preview_id"]), preview["expected_version"])
    retry = authoring.commit(scope, world_id, UUID(preview["preview_id"]), preview["expected_version"])
    assert first["duplicate"] is False
    assert retry["duplicate"] is True
    after = repository.read_world(scope, world_id, branch_id)
    return after


def _advance_claimed(repository, jobs, scope, job_id, worker_label):
    lease = repository.claim_job(worker_label)
    assert lease is not None and lease.job_id == job_id
    return jobs.advance(scope, lease)


def test_named_market_savings_resumes_across_author_listing_revisions_and_replays(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'work-purchase-resume.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    operations = OperationsStore(tmp_path / "operations.db")
    authoring = AuthoringApplication(repository, operations)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        state, plan, worker_account_id, employer_account_id, stock_account_id, seller_id, workplace_id, listing_id = _state()
        created = repository.create_world(scope, NewWorld(title="Purchase savings resume", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        job = jobs.enqueue(
            scope, world_id, branch_id, plan,
            idempotency_key="named-purchase-savings", speed="balanced", depth="deep",
        )
        retry = jobs.enqueue(
            scope, world_id, branch_id, plan,
            idempotency_key="named-purchase-savings", speed="balanced", depth="deep",
        )
        assert retry.job_id == job.job_id

        first = _advance_claimed(repository, jobs, scope, job.job_id, "first-purchase-savings-shift")
        assert first.outcome == "advanced"
        assert first.next_tick == DAY
        payment, = [event for event in first.events if isinstance(event, WorkShiftPaidEvent)]
        assert payment.plan.ledger_entries[0].amount == WAGE
        after_pay = repository.read_world(scope, world_id, branch_id)
        assert after_pay.state.current_tick == DAY
        assert after_pay.state.resources[worker_account_id].balance == INITIAL_WALLET + WAGE
        assert after_pay.state.entities[workplace_id].components["market"]["offers"][0]["unit_price_minor"] == HIGH_PRICE
        target = plan.work_schedule.purchase_target
        assert target is not None
        assert observe_work_purchase_quote(after_pay.state, target) == Decimal("300")
        expected_progress = {
            "shifts_paid": 1,
            "wages_by_resource": {RESOURCE_ID: "20.00"},
            "last_paid_tick": str(DAY),
            "balance_resource_id": RESOURCE_ID,
            "balance_after_last_pay": "22.25",
        }
        assert repository.read_job(scope, job.job_id).payload["work_progress"] == expected_progress

        repository.control_job(scope, job.job_id, "pause")
        deactivated = _revise_listing(
            authoring, repository, scope, world_id, branch_id, workplace_id,
            active=False, price=HIGH_PRICE, reason="Temporarily deactivate the grain listing.",
        )
        assert deactivated.state.current_tick == DAY
        assert deactivated.state.entities[workplace_id].components["market"]["offers"][0]["active"] is False

        repository.control_job(scope, job.job_id, "resume")
        unavailable = _advance_claimed(repository, jobs, scope, job.job_id, "unavailable-purchase-resume")
        assert unavailable.outcome == "stopped"
        assert unavailable.metrics.integration_boundary == "work_purchase_target_unavailable"
        assert unavailable.interruption.reason_code == "work_purchase_target_unavailable"
        assert unavailable.events == ()
        paused = repository.read_job(scope, job.job_id)
        assert paused.status == JobStatus.PAUSED
        assert paused.payload["plan"] == plan.model_dump(mode="json")
        assert paused.payload["work_progress"] == expected_progress
        after_unavailable = repository.read_world(scope, world_id, branch_id)
        assert after_unavailable.state_hash == deactivated.state_hash
        assert after_unavailable.state.current_tick == DAY
        assert after_unavailable.state.resources[worker_account_id].balance == INITIAL_WALLET + WAGE

        restored = _revise_listing(
            authoring, repository, scope, world_id, branch_id, workplace_id,
            active=True, price=LOW_PRICE, reason="Restore the grain listing at the reviewed lower price.",
        )
        assert restored.state.current_tick == DAY
        assert restored.state.entities[workplace_id].components["market"]["offers"][0]["active"] is True
        assert restored.state.entities[workplace_id].components["market"]["offers"][0]["unit_price_minor"] == LOW_PRICE

        repository.control_job(scope, job.job_id, "resume")
        finished = _advance_claimed(repository, jobs, scope, job.job_id, "affordable-purchase-resume")
        assert finished.outcome == "completed"
        assert finished.metrics.integration_boundary == "work_purchase_target_affordable"
        assert finished.events == ()

        saved = repository.read_job(scope, job.job_id)
        assert saved.status == JobStatus.SUCCEEDED
        assert saved.payload["plan"] == plan.model_dump(mode="json")
        assert saved.payload["work_progress"] == expected_progress
        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == DAY
        assert final.state.resources[worker_account_id].balance == INITIAL_WALLET + WAGE
        assert final.state.resources[employer_account_id].balance == Decimal("80.00")
        assert final.state.resources[stock_account_id].balance == INITIAL_STOCK
        assert final.state.entities[seller_id].components["inventory"]["lots"][0]["quantity"] == str(INITIAL_STOCK)
        event_rows = repository.list_events(scope, world_id, branch_id, limit=100).events
        assert sum(row.type == "core.work_shift_paid.v1" for row in event_rows) == 1
        assert not any(row.type == "core.market_trade_executed.v1" for row in event_rows)
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
    finally:
        database.dispose()


def test_crash_after_wage_commit_recovers_affordability_without_repaying(tmp_path, monkeypatch):
    from world_engine.storage import repository as repository_module

    database = Database(f"sqlite:///{(tmp_path / 'work-purchase-crash.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    fixed_now = [datetime(2040, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(repository_module, "utc_now", lambda: fixed_now[0])
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        state, plan, worker_account_id, employer_account_id, stock_account_id, seller_id, workplace_id, _listing_id = _state(
            price=JUST_AFFORDABLE_PRICE,
        )
        created = repository.create_world(scope, NewWorld(title="Crash-safe purchase savings", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        job = jobs.enqueue(
            scope, world_id, branch_id, plan,
            idempotency_key="crash-safe-purchase-savings", speed="balanced", depth="deep",
        )
        target = plan.work_schedule.purchase_target
        assert target is not None
        quote = observe_work_purchase_quote(state, target)
        assert quote == Decimal("21")
        assert INITIAL_WALLET < quote < INITIAL_WALLET + WAGE

        lease = repository.claim_job("worker-before-process-stop")
        assert lease is not None and lease.job_id == job.job_id
        class SimulatedProcessStop(BaseException):
            pass

        def stop_before_job_finish(*_args, **_kwargs):
            raise SimulatedProcessStop()

        with monkeypatch.context() as crash_patch:
            crash_patch.setattr(repository, "finish_job", stop_before_job_finish)
            with pytest.raises(SimulatedProcessStop):
                jobs.advance(scope, lease)

        after_crash = repository.read_world(scope, world_id, branch_id)
        assert after_crash.state.current_tick == DAY
        assert after_crash.state.resources[worker_account_id].balance == INITIAL_WALLET + WAGE
        assert after_crash.state.resources[employer_account_id].balance == Decimal("80.00")
        assert observe_work_purchase_quote(after_crash.state, target) == quote
        assert after_crash.state.resources[worker_account_id].balance >= quote
        interrupted = repository.read_job(scope, job.job_id)
        expected_progress = {
            "shifts_paid": 1,
            "wages_by_resource": {RESOURCE_ID: "20.00"},
            "last_paid_tick": str(DAY),
            "balance_resource_id": RESOURCE_ID,
            "balance_after_last_pay": "22.25",
        }
        assert interrupted.status == JobStatus.RUNNING
        assert interrupted.result is None
        assert interrupted.committed_through_tick == DAY
        assert interrupted.payload["committed_state_version"] == after_crash.branch.state_version
        assert interrupted.payload["work_progress"] == expected_progress
        assert interrupted.fencing_token == lease.fencing_token == 1

        event_rows = repository.list_events(scope, world_id, branch_id, limit=100).events
        assert sum(row.type == "core.work_shift_paid.v1" for row in event_rows) == 1
        assert not any(row.type == "core.market_trade_executed.v1" for row in event_rows)

        fixed_now[0] += timedelta(seconds=61)
        recovered_lease = repository.claim_job("worker-after-process-stop")
        assert recovered_lease is not None and recovered_lease.job_id == job.job_id
        assert recovered_lease.fencing_token == lease.fencing_token + 1
        recovered = jobs.advance(scope, recovered_lease)

        assert recovered.outcome == "completed"
        assert recovered.metrics.integration_boundary == "work_purchase_target_affordable"
        assert recovered.next_tick == DAY
        assert recovered.events == ()
        completed = repository.read_job(scope, job.job_id)
        assert completed.status == JobStatus.SUCCEEDED
        assert completed.payload["plan"] == plan.model_dump(mode="json")
        assert completed.payload["work_progress"] == expected_progress
        assert completed.result["metrics"]["integration_boundary"] == "work_purchase_target_affordable"
        assert completed.result["next_tick"] == DAY

        final = repository.read_world(scope, world_id, branch_id)
        assert final.state_hash == after_crash.state_hash
        assert final.state.current_tick == DAY
        assert final.state.resources[worker_account_id].balance == INITIAL_WALLET + WAGE
        assert final.state.resources[employer_account_id].balance == Decimal("80.00")
        assert final.state.resources[stock_account_id].balance == INITIAL_STOCK
        assert final.state.entities[seller_id].components["inventory"]["lots"][0]["quantity"] == str(INITIAL_STOCK)
        event_rows = repository.list_events(scope, world_id, branch_id, limit=100).events
        assert sum(row.type == "core.work_shift_paid.v1" for row in event_rows) == 1
        assert not any(row.type == "core.market_trade_executed.v1" for row in event_rows)
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
    finally:
        database.dispose()
