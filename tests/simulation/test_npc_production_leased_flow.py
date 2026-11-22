import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select

from world_engine.adapters.models import RoleTask
from world_engine.application.backups import BackupApplication
from world_engine.application.model_decisions import invoke_npc_planning
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import GoalRecord
from world_engine.domain.models import AccessScope, NewWorld, ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NPC_SHARED_PRODUCTION_POLICY, occupied_npc_ids
from world_engine.domain.reducers import apply_event
from world_engine.simulation import WorldAdvancePlan
from world_engine.simulation.contracts import SimulationContext
from world_engine.simulation.npc_plan_execution import build_npc_plan_execution_transition
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.models import LedgerPostingRow
from world_engine.storage.repository import WorldRepository


def test_shared_production_competes_for_finite_inputs_at_settlement(tmp_path):
    world, branch, place, player, worker_a, worker_b = (uuid4() for _ in range(6))
    stock, output, quota, sink, lot = (uuid4() for _ in range(5))
    workers = {worker_a, worker_b}
    goals = {worker: uuid4() for worker in workers}
    recipe = "recipe.core.tools"
    shop = WorldEntity(
        id=place, kind="place", name="Workshop",
        components={
            "production": {"recipes": [{
                "recipe_id": recipe, "duration_ticks": 600,
                "inputs": [{"resource_id": "core.wood", "quantity": "2"}],
                "outputs": [{"resource_id": "core.tool", "quantity": "1"}],
            }]},
            "inventory": {"lots": [{
                "lot_id": str(lot), "account_id": str(stock), "resource_id": "core.wood", "quantity": "2",
            }]},
        },
    )
    state = WorldState(
        player_id=player, metadata={NPC_SHARED_PRODUCTION_POLICY: True},
        entities={place: shop, **{
            character: WorldEntity(id=character, kind="character", name="Craftsperson", location_id=place)
            for character in {*workers, player}
        }},
        resources={
            stock: ResourceAccount(resource_id="core.wood", balance="2", owner_id=place),
            output: ResourceAccount(resource_id="core.tool", balance="0", owner_id=place),
            quota: ResourceAccount(resource_id="core.tool", balance="0", owner_id=place,
                                   account_type="production_source", credit_limit="2"),
            sink: ResourceAccount(resource_id="core.wood", balance="0", account_type="consumption_sink"),
        },
        goals=[
            GoalRecord(id=goal, owner_id=actor, title="Make tools", created_tick=0, updated_tick=0)
            for actor, goal in goals.items()
        ],
    )
    state = state.model_copy(update={"scheduled_events": [
        build_npc_plan_review_schedule(state, world, worker, 0) for worker in sorted(workers, key=str)
    ]})
    database = Database(f"sqlite:///{(tmp_path / 'workshop.db').as_posix()}")
    restored_db = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    database.create_schema()
    restored_db.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())

    class FixedProductionPlanner:
        async def generate(self, _scope, request):
            if request.role_task is RoleTask.NPC_PLAN_REVIEW:
                return SimpleNamespace(normalized_output={"verdict": "approve"})
            assert request.role_task is RoleTask.NPC_DECISION
            context = json.loads(request.messages[1].content)
            assert context["shared_production_available"]
            workplace, = context["known_view"]["available_production"]
            assert workplace["producer_id"] == str(place)
            known_recipe, = workplace["recipes"]
            assert known_recipe["inputs"] == [{"resource_id": "core.wood", "quantity": "2"}]
            return SimpleNamespace(normalized_output={
                "kind": "plan", "goal_ids": [str(goals[UUID(context["actor"]["id"])])],
                "steps": [{"capability_id": "world.action.produce.v1", "arguments": {
                    "producer_id": str(place), "recipe_id": recipe, "batch_count": 1,
                }}],
                "reason": "Prepare one registered batch.",
            })

    try:
        repo = WorldRepository(database)
        repo.create_world(scope, NewWorld(world_id=world, branch_id=branch, title="Workshop", state=state))
        jobs = SimulationJobs(repo)
        job = jobs.enqueue_world_advance(
            scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=600),
            idempotency_key="two-workers-one-batch", speed="balanced", depth="standard",
        )
        lease = repo.claim_job("workshop", job_type=job.job_type, lease_seconds=180)
        assert lease is not None
        events = []
        backups = BackupApplication(repo)
        active_archive = None
        active_hash = None
        for _ in range(18):
            view = repo.read_world(scope, world, branch)
            if active_archive is None and view.state.current_tick == 0 and occupied_npc_ids(view.state, 0) == workers:
                active_archive = backups.export_world(scope, world)
                active_hash = view.state_hash
                assert {key: row.balance for key, row in view.state.resources.items()} == {
                    key: row.balance for key, row in state.resources.items()
                }
                # Changing the recipe mid-work invalidates the original conversion.
                previous = shop.components["production"]["recipes"][0]
                revised_shop = shop.model_copy(update={"components": shop.components | {
                    "production": {"recipes": [previous | {"outputs": [{"resource_id": "core.tool", "quantity": "2"}]}]},
                }})
                altered = view.state.model_copy(update={"entities": view.state.entities | {place: revised_shop}})
                due = apply_event(altered, TimeAdvancedEvent(to_tick=600))
                settlement = build_npc_plan_execution_transition(
                    due, due.npc_plans[0].plan_id, SimulationContext(
                        world_id=world, branch_id=branch, command_id=uuid4(), world_seed="workshop",
                        state_version=view.branch.state_version,
                    ),
                )
                interrupted, = settlement.events
                assert interrupted.production_result == "recipe_changed" and interrupted.plan.status == "blocked"
            if repo.read_job(scope, job.job_id).status.value == "succeeded":
                break
            result = jobs.advance(scope, lease)
            if result.decision_requests:
                request, = result.decision_requests
                answer = asyncio.run(invoke_npc_planning(FixedProductionPlanner(), repo, scope, lease, job, request))
                assert answer.npc_planning_status == "ready"
                result = jobs.advance(scope, lease, model_decision=answer)
            events.extend(result.events)
        assert repo.read_job(scope, job.job_id).status.value == "succeeded"
        assert active_archive is not None
        assert [event.to_tick for event in events if event.type == "core.time_advanced.v1"] == [600]
        conversion, = [event for event in events if event.type == "core.resource_produced.v1"]
        receipts = [event for event in events if getattr(event, "production_result", None)]
        assert [event.production_result for event in receipts] == ["production_completed", "insufficient_input"]
        assert all(event.plan.updated_tick == 600 for event in receipts)
        assert receipts[0].plan.evidence_refs == [conversion.event_id, receipts[0].event_id]
        assert receipts[1].plan.current_step == 0 and not receipts[1].plan.evidence_refs
        final = repo.read_world(scope, world, branch)
        assert {key: row.balance for key, row in final.state.resources.items()} == {
            stock: Decimal("0"), output: Decimal("1"), quota: Decimal("-1"), sink: Decimal("2"),
        }
        assert repo.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash
        with database.session_factory() as session:
            postings = session.scalars(select(LedgerPostingRow).where(LedgerPostingRow.event_id == conversion.event_id)).all()
        assert sorted(Decimal(row.delta) for row in postings) == [Decimal("-2"), Decimal("-1"), Decimal("1"), Decimal("2")]
        restored = WorldRepository(restored_db)
        with backups.validate_archive(active_archive) as verified:
            WorldBackupImporter(restored).import_verified_backup(scope, verified)
        restored_opening = restored.replay(scope, world, branch, from_genesis=True)
        assert restored_opening.state_hash == active_hash and occupied_npc_ids(restored_opening.state, 0) == workers
        restored_jobs = SimulationJobs(restored)
        resumed = restored_jobs.enqueue_world_advance(
            scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=600),
            idempotency_key="restored-workshop", speed="balanced", depth="standard",
        )
        resumed_lease = restored.claim_job("restored-workshop", job_type=resumed.job_type, lease_seconds=180)
        assert resumed_lease is not None
        restored_events = []
        for _ in range(10):
            if restored.read_job(scope, resumed.job_id).status.value == "succeeded":
                break
            resolution = restored_jobs.advance(scope, resumed_lease)
            assert not resolution.decision_requests
            restored_events.extend(resolution.events)
        assert restored.read_job(scope, resumed.job_id).status.value == "succeeded"
        assert [event.production_result for event in restored_events if getattr(event, "production_result", None)] == [
            "production_completed", "insufficient_input",
        ]
        resumed_view = restored.read_world(scope, world, branch)
        assert resumed_view.state.resources == final.state.resources
        assert restored.replay(scope, world, branch, from_genesis=True).state_hash == resumed_view.state_hash
    finally:
        restored_db.dispose()
        database.dispose()
