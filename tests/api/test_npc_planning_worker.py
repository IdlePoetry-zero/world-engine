"""Worker dispatch coverage for real, leased NPC plan-review requests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
import world_engine.application.model_decisions as model_decisions
import world_engine.runtime as runtime
import world_engine.application.npc_planning as npc_planning
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import GoalCondition, GoalRecord, PlaceComponent, PlaceRoute
from world_engine.domain.models import AccessScope, NewWorld, WorldEntity, WorldState
from world_engine.domain.npc_goal_completion import NPC_STEP_GOAL_COMPLETION_POLICY
from world_engine.domain.npc_plan_review import (
    NPC_GOAL_FOLLOWUP_REVIEW_POLICY,
    NPC_MODEL_UNAVAILABLE_REASON,
    build_npc_plan_review_schedule,
)
from world_engine.domain.npc_plans import NPC_SHARED_TRAINING_POLICY, NpcPlan, NpcPlanStep
from world_engine.domain.reducers import state_hash
from world_engine.simulation.contracts import DecisionRequest, DecisionResponse, WorldAdvancePlan
from world_engine.simulation.npc_capabilities import capability_id_for_action
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _load_probe_runner():
    script = Path(__file__).resolve().parents[2] / "scripts" / "probe-world-advance.py"
    spec = importlib.util.spec_from_file_location("probe_world_advance_npc_worker_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pending_review_job(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'npc-review-worker.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    world_id, player_id, actor_id = uuid4(), uuid4(), uuid4()
    state = WorldState(
        metadata={NPC_SHARED_TRAINING_POLICY: True},
        player_id=player_id,
        entities={
            player_id: WorldEntity(id=player_id, kind="character", name="Player"),
            actor_id: WorldEntity(
                id=actor_id, kind="character", name="Ari",
                components={"skills": {"skills": {"skill.navigation": {"level": "1"}}}},
            ),
        },
        goals=[GoalRecord(
            id=uuid4(), owner_id=actor_id, title="Find a safe road", description="Reach the northern market.",
            created_tick=0, updated_tick=0,
        ), GoalRecord(
            id=uuid4(), owner_id=player_id, title="PRIVATE_UNSELECTED_GOAL_SENTINEL",
            description="Only the player knows this objective.", created_tick=0, updated_tick=0,
        )],
    )
    review = build_npc_plan_review_schedule(state, world_id, actor_id, state.current_tick)
    state = state.model_copy(update={"scheduled_events": [*state.scheduled_events, review]})
    created = repository.create_world(scope, NewWorld(
        world_id=world_id, title="NPC plan worker test", ruleset_version="npc-worker-test.v1", state=state,
    ))
    jobs = SimulationJobs(repository)
    job = jobs.enqueue_world_advance(
        scope, created.world.world_id, created.branch.branch_id,
        WorldAdvancePlan(started_tick=0, target_tick=60),
        idempotency_key="npc-plan-review-worker", speed="balanced", depth="deep",
        expected_state_version=0,
    )
    lease = repository.claim_job("npc-plan-review-test-worker", lease_seconds=120)
    assert lease is not None and lease.job_id == job.job_id
    resolution = jobs.advance(scope, lease)
    assert resolution.outcome == "decision_required"
    assert resolution.events == ()
    assert len(resolution.decision_requests) == 1
    decision = resolution.decision_requests[0]
    assert decision.kind == "npc.plan_review"
    assert decision.decision_owner == "model"
    assert decision.options[0].option_id == "plan"
    job = repository.read_job(scope, lease.job_id)
    assert job.status.value == "running"

    try:
        yield SimpleNamespace(
            scope=scope, database=database, repository=repository, jobs=jobs,
            lease=lease, job=job, decision=decision, actor_id=actor_id,
            goal_id=state.goals[0].id,
        )
    finally:
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("output", "outcome", "rejection"), [
    ({"kind": "defer", "reason": "No safe route yet."}, "defer", None),
    ({"kind": "plan", "unexpected": "Private model text."}, "invalid", "invalid_npc_plan_output"),
])
async def test_leased_npc_planning_records_job_and_decision_trace_scope(pending_review_job, output, outcome, rejection):
    context = pending_review_job

    class CapturingModel:
        async def generate(self, scope, request):
            assert request.role_task is RoleTask.NPC_DECISION
            assert scope == context.scope
            assert request.trace_context.job_id == str(context.lease.job_id)
            assert request.trace_context.parent_trace == str(context.decision.request_id)
            assert request.trace_context.branch_id == str(context.lease.branch_id)
            assert all(str(context.lease.job_id) not in message.content for message in request.messages)
            return SimpleNamespace(
                normalized_output=output,
                call_trace=SimpleNamespace(request_id="leased-npc-test-call"),
            )

    result = await model_decisions.invoke_npc_planning(
        CapturingModel(), context.repository, context.scope, context.lease, context.job, context.decision,
    )
    assert result.npc_planning_status == outcome
    assert result.npc_model_call_id == "leased-npc-test-call"
    assert result.npc_planning_rejection == rejection
    assert result.explanation == ""

    receipt = context.jobs.advance(context.scope, context.lease, model_decision=result)
    review, = receipt.events
    assert review.reason == (rejection or "npc_planning_defer")
    assert review.visibility == {"policy": "author_only"}
    saved = context.repository.read_job(context.scope, context.lease.job_id)
    assert saved.payload["last_decision"] == result.model_dump(mode="json")
    view = context.repository.read_world(context.scope, context.lease.world_id, context.lease.branch_id)
    replay = context.repository.replay(context.scope, context.lease.world_id, context.lease.branch_id, from_genesis=True)
    assert replay.state_hash == view.state_hash


@pytest.mark.asyncio
async def test_unavailable_npc_planner_records_policy_defer_and_bounded_retry(pending_review_job):
    context = pending_review_job

    class OfflineProvider:
        async def generate(self, _scope, _request):
            raise ProviderError(ProviderErrorCode.NETWORK, "provider is offline", retryable=True)

    response = await model_decisions.invoke_npc_planning(
        OfflineProvider(), context.repository, context.scope, context.lease,
        context.job, context.decision,
    )

    assert response.source == "policy"
    assert response.npc_planning_status == "defer"
    assert response.npc_planning_policy_reason == NPC_MODEL_UNAVAILABLE_REASON
    assert response.npc_plan is None and response.npc_model_call_id is None
    saved = context.jobs.advance(context.scope, context.lease, model_decision=response)
    review, = [event for event in saved.events if event.type == "core.npc_plan_review_resolved.v1"]
    assert review.source == "policy" and review.outcome == "defer"
    assert review.reason == NPC_MODEL_UNAVAILABLE_REASON and review.model_call_id is None
    assert saved.outcome == "advanced"

    view = context.repository.read_world(context.scope, context.lease.world_id, context.lease.branch_id)
    retry, = [item for item in view.state.scheduled_events if item.id == review.scheduled_event_id]
    assert retry.status == "queued" and retry.due_tick == 3_600
    assert retry.completed_runs == 1 and retry.max_runs == 2
    job = context.repository.read_job(context.scope, context.lease.job_id)
    assert job.payload["last_decision"]["npc_planning_policy_reason"] == NPC_MODEL_UNAVAILABLE_REASON
    assert context.repository.replay(
        context.scope, context.lease.world_id, context.lease.branch_id, from_genesis=True,
    ).state_hash == view.state_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("review_outcome", ["approved", "rejected", "timeout", "no_time"])
async def test_leased_ready_plan_semantic_review_is_bounded_and_records_typed_result(
    pending_review_job, monkeypatch, review_outcome,
):
    context = pending_review_job
    calls = []
    review_requests = []
    selected_goal_ids = []

    class RoleAwareModel:
        async def generate(self, scope, request):
            calls.append(request)
            assert scope == context.scope
            if request.role_task is RoleTask.NPC_DECISION:
                prompt = json.loads(request.messages[1].content)
                assert "PRIVATE_UNSELECTED_GOAL_SENTINEL" not in request.messages[1].content
                selected_goal_ids.extend(goal["id"] for goal in prompt["active_goals"])
                assert len(selected_goal_ids) == 1
                return SimpleNamespace(
                    normalized_output={
                        "kind": "plan",
                        "goal_ids": selected_goal_ids,
                        "steps": [{
                            "capability_id": capability_id_for_action("train"),
                            "arguments": {"skill_id": "skill.navigation", "duration_ticks": 600},
                        }],
                        "reason": "Practice a registered skill useful for travel.",
                    },
                    call_trace=SimpleNamespace(request_id="npc-original-proposal-call"),
                )
            assert request.role_task is RoleTask.NPC_PLAN_REVIEW
            review_requests.append(request)
            data = json.loads(request.messages[1].content)
            assert set(data) == {"selected_goals", "steps", "rationale"}
            assert [goal["id"] for goal in data["selected_goals"]] == [selected_goal_ids[0]]
            assert "PRIVATE_UNSELECTED_GOAL_SENTINEL" not in request.messages[1].content
            assert "PRIVATE_UNSELECTED_GOAL_SENTINEL" not in "\n".join(message.content for message in request.messages)
            assert data["steps"][0]["capability_id"] == capability_id_for_action("train")
            assert data["rationale"] == "Practice a registered skill useful for travel."
            if review_outcome == "timeout":
                await asyncio.sleep(0.05)
                verdict = "approve"
            else:
                verdict = "approve" if review_outcome == "approved" else "reject"
            return SimpleNamespace(
                normalized_output={"verdict": verdict},
                call_trace=SimpleNamespace(request_id="npc-semantic-review-call"),
            )

    if review_outcome == "timeout":
        monkeypatch.setattr(npc_planning.NpcPlanningApplication, "semantic_review_max_wall_seconds", 0.005)
    elif review_outcome == "no_time":
        monkeypatch.setattr(
            model_decisions, "NPC_PLAN_REVIEW_DEADLINE_RESERVE_SECONDS",
            model_decisions.MODEL_DECISION_TIMEOUT_SECONDS + 1,
        )

    response = await model_decisions.invoke_npc_planning(
        RoleAwareModel(), context.repository, context.scope, context.lease, context.job, context.decision,
    )
    if review_outcome == "approved":
        expected_status, expected_rejection = "ready", None
    else:
        expected_status = "invalid"
        expected_rejection = (
            "npc_plan_semantic_rejected" if review_outcome == "rejected"
            else "npc_plan_semantic_unavailable"
        )
    assert response.npc_planning_status == expected_status
    assert response.npc_planning_rejection == expected_rejection
    assert response.npc_model_call_id == "npc-original-proposal-call"
    assert (response.npc_plan is not None) == (expected_rejection is None)
    assert len(review_requests) == (0 if review_outcome == "no_time" else 1)
    assert calls[0].role_task is RoleTask.NPC_DECISION
    assert all(request.role_task is RoleTask.NPC_PLAN_REVIEW for request in review_requests)

    resolution = context.jobs.advance(context.scope, context.lease, model_decision=response)
    receipt, = [event for event in resolution.events if event.type == "core.npc_plan_review_resolved.v1"]
    assert receipt.model_call_id == "npc-original-proposal-call"
    assert receipt.reason == (expected_rejection or "npc_planning_ready")
    if expected_rejection is None:
        assert receipt.outcome == "ready"
        assert receipt.plan_id is not None
    else:
        assert receipt.outcome == "invalid"
        assert receipt.plan_id is None
        assert resolution.outcome == "advanced"
        continued = context.jobs.advance(context.scope, context.lease)
        assert continued.outcome == "completed"
        assert context.repository.read_job(context.scope, context.job.job_id).status.value == "succeeded"
        assert not any(plan.actor_id == context.actor_id for plan in context.repository.read_world(
            context.scope, context.lease.world_id, context.lease.branch_id,
        ).state.npc_plans)
    replayed = context.repository.replay(context.scope, context.lease.world_id, context.lease.branch_id)
    assert replayed.state_hash == context.repository.read_world(
        context.scope, context.lease.world_id, context.lease.branch_id,
    ).state_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(("depth", "expected_rounds"), [
    ("standard", 1), ("deep", 1), ("exhaustive", 2),
])
async def test_leased_semantic_review_rounds_follow_explicit_depth_profile(
    pending_review_job, depth, expected_rounds,
):
    context = pending_review_job
    reviewer_calls = []

    class RoleAwareModel:
        async def generate(self, _scope, request):
            if request.role_task is RoleTask.NPC_DECISION:
                prompt = json.loads(request.messages[1].content)
                return SimpleNamespace(
                    normalized_output={
                        "kind": "plan", "goal_ids": [prompt["active_goals"][0]["id"]],
                        "steps": [{
                            "capability_id": capability_id_for_action("train"),
                            "arguments": {"skill_id": "skill.navigation", "duration_ticks": 600},
                        }],
                        "reason": "Practice a useful skill.",
                    },
                    call_trace=SimpleNamespace(request_id="npc-depth-proposal-call"),
                )
            assert request.role_task is RoleTask.NPC_PLAN_REVIEW
            reviewer_calls.append(request)
            return SimpleNamespace(normalized_output={"verdict": "approve"})

    job = context.job.model_copy(update={"payload": context.job.payload | {"depth": depth}})
    response = await model_decisions.invoke_npc_planning(
        RoleAwareModel(), context.repository, context.scope, context.lease, job, context.decision,
    )
    assert response.npc_planning_status == "ready"
    assert len(reviewer_calls) == expected_rounds
    assert all(request.profile.depth.value == depth for request in reviewer_calls)


def test_semantic_review_trade_step_uses_only_the_exact_visible_listing_fact():
    listing_id = str(uuid4())
    other_listing_id = str(uuid4())
    visible_offer = {
        "listing_id": listing_id, "resource_id": "core.silver", "unit_price_minor": "7",
        "minimum_quantity": "2", "minimum_within_budget": True,
    }
    steps = [
        NpcPlanStep(
            capability_id=capability_id_for_action("trade"),
            arguments={"listing_id": listing_id, "quantity": "2"},
        ),
        NpcPlanStep(
            capability_id=capability_id_for_action("trade"),
            arguments={"listing_id": other_listing_id, "quantity": "1"},
        ),
    ]
    reviewed = npc_planning._semantic_review_steps(steps, {
        "known_view": {"market_offers": {"offers": [visible_offer]}},
    })
    assert reviewed[0] == {
        "capability_id": capability_id_for_action("trade"),
        "arguments": {"listing_id": listing_id, "quantity": "2"},
        "supporting_facts": {"market_listing": visible_offer},
    }
    assert reviewed[1] == {
        "capability_id": capability_id_for_action("trade"),
        "arguments": {"listing_id": other_listing_id, "quantity": "1"},
    }


@pytest.mark.parametrize("automatic", [False, True], ids=["existing-review", "prerequisite-followup"])
def test_repeated_review_uses_a_committed_prerequisite_from_an_earlier_plan(tmp_path, automatic):
    world, actor, player, start, station, pier = (uuid4() for _ in range(6))
    prerequisite, delivery, private, invalid = (uuid4() for _ in range(4))
    first_route = PlaceRoute(route_id=uuid4(), from_place_id=start, to_place_id=station, travel_ticks=60)
    second_route = PlaceRoute(route_id=uuid4(), from_place_id=station, to_place_id=pier, travel_ticks=60)

    def place(key, routes):
        return WorldEntity(id=key, kind="place", components={
            "place": PlaceComponent(place_type="site", routes=routes).model_dump(mode="json"),
        })

    state = WorldState(player_id=player, metadata={
        NPC_STEP_GOAL_COMPLETION_POLICY: True, NPC_GOAL_FOLLOWUP_REVIEW_POLICY: automatic,
    }, entities={
        start: place(start, [first_route]), station: place(station, [second_route]), pier: place(pier, []),
        actor: WorldEntity(id=actor, kind="character", location_id=start, components={
            "known_places": [str(pier)], "capabilities": {"grants": ["world.travel"]},
        }),
        player: WorldEntity(id=player, kind="character", location_id=start),
    }, goals=[
        GoalRecord(id=delivery, owner_id=actor, title="Reach the pier after the station", conditions=[
            GoalCondition(kind="goal_completed", target_id=prerequisite),
            GoalCondition(kind="reach_location", subject_id=actor, target_id=pier),
        ]),
        GoalRecord(id=prerequisite, owner_id=actor, title="Reach the station", conditions=[
            GoalCondition(kind="reach_location", subject_id=actor, target_id=station),
        ]),
        GoalRecord(id=private, owner_id=player, title="PRIVATE_PREREQUISITE", status="succeeded", evidence_refs=[uuid4()]),
        GoalRecord(id=invalid, owner_id=actor, title="Unknowable dependency", conditions=[
            GoalCondition(kind="goal_completed", target_id=private),
        ]),
    ])
    schedule = build_npc_plan_review_schedule(state, world, actor, 0,
                                              repeat_ticks=None if automatic else 120, max_runs=1 if automatic else 2)
    state = state.model_copy(update={"scheduled_events": [schedule]})
    database = Database(f"sqlite:///{(tmp_path / 'repeated-review.sqlite').as_posix()}")
    database.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    repository = WorldRepository(database)
    jobs = SimulationJobs(repository)
    observed = []

    class CapturingModel:
        async def generate(self, scope, request):
            if request.role_task is RoleTask.NPC_PLAN_REVIEW:
                return SimpleNamespace(normalized_output={"verdict": "approve"})
            if request.role_task is RoleTask.MEMORY_SUMMARY:
                return SimpleNamespace(normalized_output={"outlook": "defer"})
            assert request.role_task is RoleTask.NPC_DECISION
            prompt = request.messages[1].content
            data = json.loads(prompt)
            active = {goal["id"]: goal for goal in data["active_goals"]}
            assert str(private) not in prompt and str(invalid) not in prompt
            condition = active[str(delivery)]["conditions"][0]
            observed.append((data["known_view"]["tick"], condition["prerequisite_status"], condition["prerequisite_updated_tick"]))
            if len(observed) == 1:
                goal, route = prerequisite, first_route
            else:
                assert str(prerequisite) not in active
                goal, route = delivery, second_route
            return SimpleNamespace(normalized_output={
                "kind": "plan", "goal_ids": [str(goal)], "reason": "Follow this route.",
                "steps": [{"capability_id": capability_id_for_action("move"), "arguments": {
                    "route_id": str(route.route_id), "destination_id": str(route.to_place_id),
                }}],
            })

    try:
        view = repository.create_world(scope, NewWorld(world_id=world, title="Sequential NPC plans", ruleset_version="1.0.0", state=state))
        branch = view.branch.branch_id
        followup_tick = 180 if automatic else 120
        arrival_tick = followup_tick + 60
        job = jobs.enqueue_world_advance(scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=arrival_tick),
                                         idempotency_key="successive-reviews", speed="balanced", depth="standard")
        lease = repository.claim_job("successive-reviews", lease_seconds=180)
        assert lease is not None
        for _ in range(24):
            prior = jobs.advance(scope, lease)
            job = repository.read_job(scope, job.job_id)
            if job.status.value == "succeeded":
                break
            for request in prior.decision_requests:
                response = asyncio.run(runtime._invoke_worker_model_decision(
                    CapturingModel(), repository, scope, lease, job, request,
                ))
                if request.kind == "npc.reflection":
                    assert response is None
                    continue
                assert response.npc_planning_status == "ready", response.npc_planning_rejection
                jobs.advance(scope, lease, model_decision=response)
        assert job.status.value == "succeeded"
        assert observed == [(0, "active", 0), (followup_tick, "succeeded", 60)]
        final = repository.read_world(scope, world, branch)
        assert final.state.entities[actor].location_id == pier
        goals = {goal.id: goal for goal in final.state.goals}
        assert (goals[prerequisite].status, goals[prerequisite].updated_tick) == ("succeeded", 60)
        assert (goals[delivery].status, goals[delivery].updated_tick) == ("succeeded", arrival_tick)
        assert goals[prerequisite].evidence_refs != goals[delivery].evidence_refs
        assert goals[invalid].status == "active"
        followup_schedule = next(row for row in final.state.scheduled_events if row.id == schedule.id)
        assert followup_schedule.completed_runs == followup_schedule.max_runs == 2
        markers = [row.payload["goal_followup_review_tick"] for row in repository.list_events(scope, world, branch).events
                   if "goal_followup_review_tick" in row.payload]
        assert markers == ([followup_tick] if automatic else [])
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash
    finally:
        database.dispose()


def _typed_response(context, *, status: str, source: str = "model") -> DecisionResponse:
    plan = None
    if status == "ready":
        plan = NpcPlan(
            plan_id=uuid4(), actor_id=context.actor_id, goal_ids=[context.goal_id], revision=1,
            status="ready", created_tick=context.decision.at_tick, updated_tick=context.decision.at_tick,
            next_review_tick=context.decision.at_tick,
            steps=[NpcPlanStep(capability_id=capability_id_for_action("explore"), arguments={})],
            current_step=0, reason="A bounded test plan.",
        )
    view = context.repository.read_world(
        context.scope, context.lease.world_id, context.lease.branch_id,
    )
    return DecisionResponse(
        request_id=context.decision.request_id, state_version=context.decision.state_version,
        at_tick=context.decision.at_tick, source=source, option_id="plan",
        explanation=f"npc_planning_{status}", npc_planning_status=status, npc_plan=plan,
        npc_plan_state_hash=state_hash(view.state),
        npc_model_call_id="npc-plan-test-call" if source == "model" else None,
    )


@pytest.mark.parametrize(
    ("status", "source"),
    [("ready", "model"), ("defer", "model"), ("invalid", "model"), ("defer", "policy")],
)
@pytest.mark.parametrize("dispatcher", ["runtime", "probe"])
@pytest.mark.asyncio
async def test_leased_review_routes_to_typed_planner_and_preserves_provenance(
    pending_review_job, monkeypatch, dispatcher, status, source,
):
    context = pending_review_job
    runner = _load_probe_runner()
    response = _typed_response(context, status=status, source=source)
    selected = []

    async def plan_spy(operations, repository, scope, lease, job, decision):
        selected.append((operations, repository, scope, lease, job, decision))
        return response

    async def ordinary_spy(*_args, **_kwargs):
        raise AssertionError("NPC plan reviews must not use the enum-option decision path")

    monkeypatch.setattr(model_decisions, "invoke_npc_planning", plan_spy)
    if dispatcher == "runtime":
        monkeypatch.setattr(runtime, "invoke_model_decision", ordinary_spy)
        actual = await runtime._invoke_worker_model_decision(
            "metered-operations", context.repository, context.scope, context.lease,
            context.job, context.decision,
        )
    else:
        monkeypatch.setattr(runner, "invoke_model_decision", ordinary_spy)
        actual = await runner._invoke_probe_model_decision(
            "metered-operations", context.repository, context.scope, context.lease,
            context.job, context.decision,
        )

    assert actual is response
    assert actual.npc_planning_status == status
    assert actual.source == source
    assert (actual.npc_model_call_id is not None) is (source == "model")
    assert len(selected) == 1
    operations, repository, scope, lease, job, decision = selected[0]
    assert operations == "metered-operations"
    assert repository is context.repository
    assert scope == context.scope
    assert lease == context.lease
    assert job == context.job
    assert decision == context.decision


@pytest.mark.asyncio
async def test_ordinary_choice_keeps_legacy_dispatch_without_planner_import(pending_review_job, monkeypatch):
    context = pending_review_job
    runner = _load_probe_runner()
    ordinary = context.decision.model_copy(update={"kind": "organization.succession"})
    expected = SimpleNamespace(source="model", option_id="plan")
    calls = []

    async def ordinary_spy(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.delattr(model_decisions, "invoke_npc_planning", raising=False)
    monkeypatch.setattr(runtime, "invoke_model_decision", ordinary_spy)
    actual = await runtime._invoke_worker_model_decision(
        "metered-operations", context.repository, context.scope, context.lease,
        context.job, ordinary,
    )
    assert actual is expected
    assert len(calls) == 1
    assert calls[0][0] == ("metered-operations", context.scope, context.lease, context.job, ordinary)

    calls.clear()
    monkeypatch.setattr(runner, "invoke_model_decision", ordinary_spy)
    actual = await runner._invoke_probe_model_decision(
        "metered-operations", context.repository, context.scope, context.lease,
        context.job, ordinary,
    )
    assert actual is expected
    assert len(calls) == 1
    assert calls[0][0] == ("metered-operations", context.scope, context.lease, context.job, ordinary)
    assert calls[0][1] == {"timeout_seconds": 100}


@pytest.mark.asyncio
async def test_probe_deadline_distinguishes_timer_expiry_from_other_interruptions(monkeypatch):
    runner = _load_probe_runner()
    decision = SimpleNamespace(kind="governance.succession")

    async def wait_for_provider(*args, timeout_seconds):
        async with asyncio.timeout(timeout_seconds):
            await asyncio.Event().wait()

    monkeypatch.setattr(runner, "invoke_model_decision", wait_for_provider)
    monkeypatch.setattr(runner, "MODEL_DECISION_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(runner.ProbeModelDeadline):
        await runner._invoke_probe_model_decision(None, None, None, None, None, decision)

    monkeypatch.setattr(runner, "MODEL_DECISION_TIMEOUT_SECONDS", 100)
    waiting = asyncio.create_task(runner._invoke_probe_model_decision(None, None, None, None, None, decision))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    async def adapter_timeout(*args, **kwargs):
        raise TimeoutError("adapter")

    monkeypatch.setattr(runner, "invoke_model_decision", adapter_timeout)
    with pytest.raises(TimeoutError, match="adapter") as caught:
        await runner._invoke_probe_model_decision(None, None, None, None, None, decision)
    assert type(caught.value) is TimeoutError

