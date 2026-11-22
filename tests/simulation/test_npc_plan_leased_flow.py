from uuid import uuid4, uuid5

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.models import AccessScope, WorldState
from world_engine.domain.reducers import state_hash
from world_engine.domain.npc_plans import NpcPlan
from world_engine.simulation import WorldAdvancePlan
from world_engine.simulation.contracts import DecisionResponse
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository

from test_npc_review_engine import review_world


def test_actual_leased_review_departure_arrival_and_plan_completion_share_one_clock(tmp_path, review_world):
    draft, state, actor_id, goal_id, step = review_world
    raw = state.model_dump(mode="python")
    raw["scheduled_events"][0].update(max_runs=1, repeat_ticks=None)
    initial = WorldState.model_validate(raw)
    route_id = str(step.arguments["route_id"])
    origin_id = initial.entities[actor_id].location_id
    route = next(item for item in initial.entities[origin_id].components["place"]["routes"]
                 if item["route_id"] == route_id)
    target = route["travel_ticks"]
    db = Database(f"sqlite:///{(tmp_path / 'npc-flow.db').as_posix()}")
    db.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        repository = WorldRepository(db)
        repository.create_world(scope, draft.model_copy(update={"state": initial}))
        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope, draft.world_id, draft.branch_id, WorldAdvancePlan(started_tick=0, target_tick=target),
            idempotency_key="npc-leased-flow", speed="balanced", depth="standard",
        )
        lease = repository.claim_job("npc-flow", job_type=job.job_type)
        pending = jobs.advance(scope, lease)
        request, = pending.decision_requests
        reviewed = repository.read_world(scope, draft.world_id, draft.branch_id)
        # Deterministic test policy; this is not presented as an LLM result.
        plan = NpcPlan(
            plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor_id}:{request.state_version}"),
            actor_id=actor_id, goal_ids=[goal_id], revision=1, status="ready", created_tick=0,
            updated_tick=0, next_review_tick=0, current_step=0, steps=[step],
            reason="A deterministic route policy used to test the real worker transaction.",
        )
        answer = DecisionResponse(
            request_id=request.request_id, state_version=request.state_version, at_tick=0,
            source="policy", option_id="plan", npc_planning_status="ready", npc_plan=plan,
            npc_plan_state_hash=state_hash(reviewed.state),
        )
        jobs.advance(scope, lease, model_decision=answer)
        created = repository.read_world(scope, draft.world_id, draft.branch_id)
        assert created.state.current_tick == 0 and created.state.npc_plans[0].status == "ready"
        jobs.advance(scope, lease)
        departed = repository.read_world(scope, draft.world_id, draft.branch_id)
        assert departed.state.current_tick == 0
        assert departed.state.entities[actor_id].location_id is None
        assert departed.state.travel_journeys[0].from_place_id == origin_id
        assert departed.state.npc_plans[0].active_execution is not None
        for _ in range(20):
            current = repository.read_job(scope, job.job_id)
            if current.status != "running":
                break
            jobs.advance(scope, lease)
        assert repository.read_job(scope, job.job_id).status == "succeeded"
        after = repository.read_world(scope, draft.world_id, draft.branch_id)
        assert after.state.current_tick == target
        assert str(after.state.entities[actor_id].location_id) == step.arguments["destination_id"]
        assert after.state.npc_plans[0].status == "completed"
        assert after.state.npc_plans[0].active_execution is None
        assert after.state.npc_plans[0].evidence_refs
        journey, = after.state.travel_journeys
        assert journey.status == "arrived" and journey.arrival_tick == target
        receipt = repository.find_journey_resolution_events(scope, draft.world_id, draft.branch_id, {journey.journey_id})
        assert receipt[journey.journey_id].operation == "arrive"
        assert receipt[journey.journey_id].event_id in after.state.npc_plans[0].evidence_refs
        replayed = repository.replay(scope, draft.world_id, draft.branch_id, from_genesis=True)
        assert replayed.state_hash == after.state_hash
        assert replayed.state.current_tick == target
        assert state_hash(initial) == state_hash(state.model_copy(update={"scheduled_events": initial.scheduled_events}))
    finally:
        db.dispose()
