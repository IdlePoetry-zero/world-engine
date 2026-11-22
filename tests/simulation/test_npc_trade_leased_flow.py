import asyncio
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import json
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from world_engine.adapters.models import RoleTask
from world_engine.application.backups import BackupApplication
from world_engine.application.model_decisions import invoke_npc_planning
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import GoalCondition, GoalRecord
from world_engine.domain.models import AccessScope, NewWorld, ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.npc_goal_completion import (
    NPC_GOAL_COMPLETION_POLICY, NPC_GOAL_DEPENDENCY_COMPLETION_POLICY, NPC_STEP_GOAL_COMPLETION_POLICY,
)
from world_engine.domain.npc_plan_review import NPC_FAILURE_REVIEW_POLICY, build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NPC_SHARED_EXPLORATION_POLICY, NPC_SHARED_TRADE_POLICY, NpcPlan, NpcPlanStep, occupied_npc_ids
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.simulation.contracts import DecisionResponse, SimulationContext, WorldAdvancePlan
from world_engine.simulation.npc_plan_execution import build_npc_plan_execution_transition
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.models import LedgerPostingRow
from world_engine.storage.repository import WorldRepository
from world_engine.runtime import _invoke_worker_model_decision


@pytest.mark.parametrize("compact,replan,followup", [
    (False, False, None), (True, False, None), (True, True, None),
    (True, False, "plan_finish"), (True, False, "each_step"), (True, False, "dependencies"),
], ids=["legacy-ledger", "compact-ledger", "compact-replan", "old-multistep", "step-goal-fulfillment", "linked-goals"])
def test_two_npc_buyers_compete_for_last_stock_under_one_lease(tmp_path, compact, replan, followup):
    """A deliberately scarce market tests contention, not open-world model behavior."""
    world, place, player, seller, first, second, listing = [uuid4() for _ in range(7)]
    stock, income, first_cash, second_cash = [uuid4() for _ in range(4)]
    buyers = {first: first_cash, second: second_cash}

    def person(actor, lots=()):
        return WorldEntity(id=actor, kind="character", location_id=place, components={
            "inventory": {"lots": [
                {"lot_id": str(uuid4()), "account_id": str(account), "resource_id": resource, "quantity": amount}
                for account, resource, amount in lots
            ]},
        })

    market = WorldEntity(id=place, kind="place", name="Scarce market", components={"market": {
        "currency_resource_id": "core.coin", "offers": [{
            "listing_id": str(listing), "resource_id": "core.ore", "stock_account_id": str(stock),
            "seller_id": str(seller), "unit_price_minor": 3,
        }],
    }})
    goals = {actor: uuid4() for actor in buyers}
    step_completion = followup in {"each_step", "dependencies"}
    dependent_goals = {actor: uuid4() for actor in buyers} if followup == "dependencies" else {}
    later_goals = [
        GoalRecord(id=goal, owner_id=actor, title="Finish the sourcing milestone", conditions=[
            GoalCondition(kind="goal_completed", target_id=goals[actor]),
        ]) for actor, goal in dependent_goals.items()
    ]
    purchase_goals = [
        GoalRecord(
            id=goal, owner_id=actor, title="Buy ore", deadline=300 if step_completion else None,
            conditions=[GoalCondition(
                kind="resource_balance_minimum", subject_id=actor, resource_id="core.ore", quantity="1",
            )],
        )
        for actor, goal in goals.items()
    ]
    state = WorldState(
        player_id=player, metadata={
            NPC_SHARED_TRADE_POLICY: True, NPC_SHARED_EXPLORATION_POLICY: replan or followup is not None,
            NPC_FAILURE_REVIEW_POLICY: replan, NPC_GOAL_COMPLETION_POLICY: compact and not step_completion,
            NPC_STEP_GOAL_COMPLETION_POLICY: step_completion,
            NPC_GOAL_DEPENDENCY_COMPLETION_POLICY: bool(dependent_goals),
        },
        entities={
            place: market, player: person(player),
            seller: person(seller, [(stock, "core.ore", "1"), (income, "core.coin", "2")]),
            **{actor: person(actor, [(cash, "core.coin", "10")]) for actor, cash in buyers.items()},
        },
        resources={
            stock: ResourceAccount(resource_id="core.ore", balance="1", owner_id=seller),
            income: ResourceAccount(resource_id="core.coin", balance="2", owner_id=seller),
            **{cash: ResourceAccount(resource_id="core.coin", balance="10", owner_id=actor) for actor, cash in buyers.items()},
        },
        goals=[*later_goals, *purchase_goals],
    )
    state = state.model_copy(update={"scheduled_events": [
        build_npc_plan_review_schedule(state, world, actor, 0) for actor in buyers
    ]})
    db = Database(f"sqlite:///{(tmp_path / 'market.db').as_posix()}")
    db.create_schema()
    if compact:
        assert db.enable_compact_ledger()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        repository = WorldRepository(db)
        view = repository.create_world(scope, NewWorld(world_id=world, title="Market contention", ruleset_version="1.0.0", state=state))
        branch = view.branch.branch_id
        jobs = SimulationJobs(repository)
        target = 1020 if replan else 900 if followup else 300
        job = jobs.enqueue_world_advance(
            scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=target),
            idempotency_key="market-contention", speed="balanced", depth="standard",
        )
        lease = repository.claim_job("market-contention", job_type=job.job_type, lease_seconds=180)
        assert lease is not None
        events = []
        followup_feedback = {}
        saw_overlapping_quotes = False
        for _ in range(24):
            view = repository.read_world(scope, world, branch)
            if not saw_overlapping_quotes and occupied_npc_ids(view.state, 0) == frozenset(buyers) and view.state.current_tick == 0:
                saw_overlapping_quotes = True
                assert view.state.resources == state.resources
                # Counterfactual at the same end tick: the seller asks a higher
                # price, despite the buyer still being able to afford it.
                due_state = apply_event(view.state, TimeAdvancedEvent(to_tick=300))
                market_components = {
                    "market": {**market.components["market"], "offers": [
                        {**market.components["market"]["offers"][0], "unit_price_minor": 4},
                    ]},
                }
                due_state = due_state.model_copy(update={"entities": {
                    **due_state.entities, place: market.model_copy(update={"components": market_components}),
                }})
                price_check = build_npc_plan_execution_transition(
                    due_state, due_state.npc_plans[0].plan_id, SimulationContext(
                        world_id=world, branch_id=branch, command_id=uuid4(),
                        command_actor_id=player, world_seed="test", state_version=view.branch.state_version + 1,
                    ),
                )
                rejected, = price_check.events
                assert rejected.trade_result == "market_quote_changed"
                assert apply_event(due_state, rejected).resources == view.state.resources
            if repository.read_job(scope, job.job_id).status.value == "succeeded":
                break
            result = jobs.advance(scope, lease)
            if result.decision_requests:
                request, = result.decision_requests
                actor = request.actor_id
                if request.kind == "npc.reflection":
                    class DeferredReflectionModel:
                        async def generate(self, scope, model_request):
                            assert model_request.role_task is RoleTask.MEMORY_SUMMARY
                            return SimpleNamespace(normalized_output={"outlook": "defer"})

                    response = asyncio.run(_invoke_worker_model_decision(
                        DeferredReflectionModel(), repository, scope, lease,
                        repository.read_job(scope, job.job_id), request,
                    ))
                    assert response is None
                    continue
                if request.at_tick == 420:
                    class TestPlanningModel:
                        async def generate(self, scope, model_request):
                            if model_request.role_task is RoleTask.NPC_PLAN_REVIEW:
                                return SimpleNamespace(normalized_output={"verdict": "approve"})
                            assert model_request.role_task is RoleTask.NPC_DECISION
                            context = json.loads(model_request.messages[1].content)
                            feedback = context["own_recent_blocked_steps"]
                            followup_feedback[actor] = feedback
                            if feedback:
                                assert feedback[0]["outcome"] == "成交前存货已不足。"
                                return SimpleNamespace(normalized_output={
                                    "kind": "plan", "goal_ids": [str(goals[actor])], "reason": "Check local alternatives.",
                                    "steps": [{"capability_id": "world.action.explore.v1", "arguments": {"place_id": str(place)}}],
                                })
                            return SimpleNamespace(normalized_output={"kind": "defer", "reason": "Already purchased."})

                    response = asyncio.run(invoke_npc_planning(
                        TestPlanningModel(), repository, scope, lease, repository.read_job(scope, job.job_id), request,
                    ))
                    events.extend(jobs.advance(scope, lease, model_decision=response).events)
                    continue
                assert request.kind == "npc.plan_review"
                assert request.at_tick == 0
                steps = [NpcPlanStep(capability_id="world.action.trade.v1", arguments={
                    "listing_id": str(listing), "quantity": "1",
                })]
                if followup:
                    steps.append(NpcPlanStep(capability_id="world.action.explore.v1", arguments={"place_id": str(place)}))
                linked_goals = [goals[actor]]
                if actor in dependent_goals:
                    linked_goals.insert(0, dependent_goals[actor])
                plan = NpcPlan(
                    plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor}:{request.state_version}"),
                    actor_id=actor, goal_ids=linked_goals, revision=1, status="ready", created_tick=0,
                    updated_tick=0, next_review_tick=0, current_step=0, steps=steps,
                    reason="Fixed policy exercising competition for the same last unit.",
                )
                result = jobs.advance(scope, lease, model_decision=DecisionResponse(
                    request_id=request.request_id, state_version=request.state_version, at_tick=0, source="policy",
                    option_id="plan", npc_planning_status="ready", npc_plan=plan, npc_plan_state_hash=state_hash(view.state),
                ))
            events.extend(result.events)
        assert repository.read_job(scope, job.job_id).status.value == "succeeded"
        assert saw_overlapping_quotes
        ticks = [event.to_tick for event in events if event.type == "core.time_advanced.v1"]
        if followup:
            assert ticks[0] == 300 and ticks[-1] == 900
            assert (301 in ticks) == step_completion
        else:
            assert ticks == ([300, 420, 1020] if replan else [300])
        exchange, = [event for event in events if event.type == "core.market_trade_executed.v1"]
        receipts = [event for event in events if getattr(event, "trade_result", None)]
        assert {receipt.trade_result for receipt in receipts} == {"trade_completed", "insufficient_stock"}
        assert len(receipts) == 2
        final = repository.read_world(scope, world, branch)
        original_trades = [plan for plan in final.state.npc_plans if plan.steps[0].capability_id == "world.action.trade.v1"]
        winner = min(original_trades, key=lambda plan: str(plan.plan_id)).actor_id
        assert exchange.buyer_id == winner
        winner_receipt = next(receipt for receipt in receipts if receipt.actor_id == winner)
        tracks_goal = compact and followup != "plan_finish"
        expected_goal_ids = [goals[winner]] if tracks_goal else []
        if winner in dependent_goals:
            expected_goal_ids.append(dependent_goals[winner])
        assert winner_receipt.fulfilled_goal_ids == tuple(sorted(expected_goal_ids, key=str))
        assert ("fulfilled_goal_ids" in winner_receipt.model_dump(mode="json")) == tracks_goal
        for goal in final.state.goals:
            fulfilled = goal.id in expected_goal_ids
            assert goal.status == ("succeeded" if fulfilled else "active")
            assert goal.progress == (100 if fulfilled else 0)
            if fulfilled:
                assert goal.updated_tick == 300 and exchange.event_id in goal.evidence_refs
                assert winner_receipt.event_id in goal.evidence_refs
        if followup:
            search, = [event for event in events if getattr(event, "exploration_result", None)]
            assert winner_receipt.plan.status == "ready"
            assert search.actor_id == winner and search.plan.status == "completed"
            assert search.plan.updated_tick == 900 and search.fulfilled_goal_ids == ()
        if replan:
            loser = next(actor for actor in buyers if actor != winner)
            assert set(followup_feedback) == {loser} and len(followup_feedback[loser]) == 1
            failure = next(receipt for receipt in receipts if receipt.actor_id == loser)
            assert failure.automatic_review_tick == 420
            schedules = {row.payload['actor_id']: row for row in final.state.scheduled_events}
            assert schedules[str(winner)].completed_runs == 1
            assert schedules[str(loser)].completed_runs == 2
            assert schedules[str(loser)].status == "completed"
            old = next(plan for plan in original_trades if plan.actor_id == loser)
            assert old.status == "cancelled"
            search, = [event for event in events if getattr(event, "exploration_result", None)]
            assert search.actor_id == loser and search.plan.status == "completed"
        assert final.state.resources[stock].balance == Decimal("0")
        assert final.state.resources[income].balance == Decimal("5")
        for actor, cash in buyers.items():
            assert final.state.resources[cash].balance == Decimal("7" if actor == winner else "10")
        assert final.state.resources[exchange.buyer_resource_account_id].balance == Decimal("1")
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash
        with db.session_factory() as session:
            rows = session.scalars(select(LedgerPostingRow).where(LedgerPostingRow.event_id == exchange.event_id)).all()
        assert len(rows) == 4 and len({row.posting_id for row in rows}) == 4
        assert {row.delta for row in rows} == {"1", "-1", "3", "-3"}
        if not compact:
            with pytest.raises(ValueError, match="empty world database"):
                db.enable_compact_ledger()
        else:
            assert not db.enable_compact_ledger()
            with pytest.raises(IntegrityError, match="committed event timestamp"), db.session_factory.begin() as session:
                bad = rows[0]
                session.execute(LedgerPostingRow.__table__.insert().values(
                    posting_id=uuid4(), event_id=bad.event_id, posting_index=99, account_id=bad.account_id,
                    resource_id=bad.resource_id, delta=bad.delta, created_at=bad.created_at + timedelta(seconds=1),
                ))
            with db.session_factory() as session:
                assert session.scalar(select(LedgerPostingRow.posting_id).where(
                    LedgerPostingRow.event_id == exchange.event_id, LedgerPostingRow.posting_index == 99,
                )) is None
        backup = BackupApplication(repository)
        for version in ("1.0", "2.0"):
            destination = Database(f"sqlite:///{(tmp_path / f'restored-{version}.db').as_posix()}")
            try:
                destination.create_schema()
                if not compact:
                    assert destination.enable_compact_ledger()
                restored = WorldRepository(destination)
                archive = backup.export_world(scope, world, format_version=version)
                validator = (
                    backup.validate_archive(archive) if version == "1.0"
                    else backup.validate_archive_file_streaming(BytesIO(archive))
                )
                with validator as verified:
                    WorldBackupImporter(restored).import_verified_backup(scope, verified)
                assert restored.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash
                with destination.session_factory() as session:
                    restored_rows = session.scalars(select(LedgerPostingRow).where(LedgerPostingRow.event_id == exchange.event_id)).all()
                assert sorted((r.posting_index, r.account_id, r.resource_id, r.delta, r.created_at) for r in restored_rows) == sorted(
                    (r.posting_index, r.account_id, r.resource_id, r.delta, r.created_at) for r in rows
                )
            finally:
                destination.dispose()
    finally:
        db.dispose()
