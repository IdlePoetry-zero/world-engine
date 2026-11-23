import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from jsonschema import Draft202012Validator
import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.travel_chat import (
    enqueue_travel_for_move,
    travel_message_key,
    travel_queue_reply,
)
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import GoalRecord, HealthComponent, NeedsComponent, PlaceComponent, PlaceRoute
from world_engine.domain.errors import Conflict
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    JobStatus,
    NewWorld,
    ResourceAccount,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NPC_SHARED_TRAINING_POLICY, NpcPlan, NpcPlanStep
from world_engine.domain.relationship_promises import (
    ReturnItemTerms,
    build_promise_deadline_schedule,
    create_return_promise,
)
from world_engine.rules import ACTION_INTENT_ADAPTER, MoveIntent, RuleContext, RuleEngine
from world_engine.simulation import LongActionPlan, WorldAdvancePlan
from world_engine.simulation.contracts import DecisionResponse
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


class SimulatedProcessLoss(BaseException):
    """Interrupt after the durable travel job exists but before chat completion."""


def _id(value: int) -> UUID:
    return UUID(int=value)


def _create_world(
    tmp_path,
    *,
    route_specs=((4, 20, ()),),
    route_risk=Decimal("0"),
    grants=("world.travel",),
    promise_boundary=False,
    promise_deadline=10,
    water_stock: str | None = None,
    water_value: int = 10,
    npc_training: bool = False,
    injured: bool = False,
):
    actor_id, origin_id, destination_id = _id(1), _id(2), _id(3)
    routes = [
        PlaceRoute(
            route_id=_id(route_id),
            from_place_id=origin_id,
            to_place_id=destination_id,
            travel_ticks=duration,
            risk=route_risk,
            is_open=True,
            access_tags=list(access_tags),
        )
        for route_id, duration, access_tags in route_specs
    ]
    actor = WorldEntity(
        id=actor_id,
        kind="character",
        name="Traveler",
        status="active",
        location_id=origin_id,
        components={"capabilities": {"grants": list(grants), "restrictions": []}},
    )
    origin = WorldEntity(
        id=origin_id,
        kind="place",
        name="Departure Shore",
        components={"place": PlaceComponent(place_type="settlement", routes=routes).model_dump(mode="json")},
    )
    destination = WorldEntity(
        id=destination_id,
        kind="place",
        name="Arrival Harbor",
        components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
    )

    promises = []
    schedules = []
    resources = {}
    if promise_boundary:
        promisee_id, recipient_account_id = _id(5), _id(6)
        promisee = WorldEntity(
            id=promisee_id,
            kind="character",
            name="Promisee",
            status="active",
            location_id=origin_id,
        )
        promise = create_return_promise(
            promise_id=_id(7),
            promisor_id=actor_id,
            promisee_id=promisee_id,
            private_text="Return one herb by tick ten.",
            terms=ReturnItemTerms(
                resource_id="core.herb",
                quantity="1",
                promisee_account_id=recipient_account_id,
            ),
            created_tick=0,
            deadline_tick=promise_deadline,
        )
        promises.append(promise)
        schedules.append(build_promise_deadline_schedule(promise))
        resources[recipient_account_id] = ResourceAccount(
            resource_id="core.herb",
            balance="0",
            owner_id=promisee_id,
            account_type="holder",
        )
    if water_stock is not None:
        actor = actor.model_copy(update={
            "components": {
                **actor.components,
                "needs": NeedsComponent(water={"value": water_value}).model_dump(mode="json"),
                "inventory": {"lots": [{"lot_id": str(_id(10)), "account_id": str(_id(8)),
                                         "resource_id": "core.water", "quantity": water_stock}]},
            },
        })
        resources[_id(8)] = ResourceAccount(
            resource_id="core.water", balance=water_stock, owner_id=actor_id, account_type="holder",
        )
        resources[_id(9)] = ResourceAccount(resource_id="core.water", balance="0", account_type="consumption_sink")

    if injured:
        actor = actor.model_copy(update={"components": actor.components | {
            "health": HealthComponent(current=5, maximum=10).model_dump(mode="json"),
            "skills": {"skills": {"skill.local_knowledge": {"level": "1"}}},
        }})
    state = WorldState(
        current_tick=0,
        player_id=actor_id,
        entities={
            actor_id: actor,
            origin_id: origin,
            destination_id: destination,
            **({promisee.id: promisee} if promise_boundary else {}),
        },
        resources=resources,
        relationship_promises=promises,
        scheduled_events=schedules,
        metadata={"resource_roles": {"water_resource_id": "core.water"}} if water_stock is not None else {},
    )
    draft = NewWorld(title="Travel chat", state=state)
    if npc_training:
        learner_id, goal_id = _id(11), _id(12)
        learner = WorldEntity(
            id=learner_id, kind="person", status="active", location_id=origin_id,
            components={"skills": {"skills": {"skill.local_knowledge": {"level": "1"}}}},
        )
        state = WorldState.model_validate(state.model_dump(mode="python") | {
            "entities": state.entities | {learner_id: learner},
            "goals": [GoalRecord(id=goal_id, owner_id=learner_id, title="Study local history")],
            "metadata": state.metadata | {NPC_SHARED_TRAINING_POLICY: True},
        })
        review = build_npc_plan_review_schedule(state, draft.world_id, learner_id, 0)
        state = state.model_copy(update={"scheduled_events": [*state.scheduled_events, review]})
        draft = draft.model_copy(update={"state": state})
    database = Database(f"sqlite:///{(tmp_path / 'travel-chat.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    created = repository.create_world(scope, draft)
    return database, repository, scope, created, {
        "actor": actor_id,
        "origin": origin_id,
        "destination": destination_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stock", ["10", "2"])
async def test_exact_chat_consumption_survives_due_work_and_worker_restart(tmp_path, monkeypatch, stock):
    database, repo, scope, created, ids = _create_world(tmp_path, promise_boundary=True, water_stock=stock)
    try:
        world, branch = created.world.world_id, created.branch.branch_id
        actor = ids["actor"]
        due, = repo.read_world(scope, world, branch).state.scheduled_events
        checkpoint = repo.commit_command(
            scope, world, branch, CommandEnvelope(actor_id=actor, idempotency_key="reach-promise-boundary",
                                                  expected_state_version=0, command_type="fixture.boundary"),
            [TimeAdvancedEvent(to_tick=due.due_tick)],
        )
        calls = []

        class Models:
            async def generate(self, scope, request):
                calls.append(request.role_task)
                output = {"action": {"kind": "consume", "need": "water", "resource_id": "core.water", "quantity": "3"},
                          "clarification": ""}
                Draft202012Validator(dict(request.output_schema)).validate(output)
                return SimpleNamespace(normalized_output=output)

        models = Models()
        actions = RulesActionDispatcher(repo, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
        chat = ConversationApplication(WorldApplication(repo, None), models,
                                       ConversationStore(OperationsStore(tmp_path / "ops.db")), actions=actions)
        message = _request(created, actor).model_copy(update={
            "text": "我现在喝三份自己的水。", "expected_state_version": str(checkpoint.state_version),
        })
        def interrupted_finish(*args, **kwargs):
            raise SimulatedProcessLoss("force loss after durable write and before the acknowledgment")

        with monkeypatch.context() as patch:
            patch.setattr(chat.store, "finish", interrupted_finish)
            with pytest.raises(SimulatedProcessLoss):
                await chat.send(scope, message)
        assert chat.store.recover_inflight() == 1
        reply = await chat.send(scope, message)
        job = repo.read_job(scope, UUID(reply["job_id"]))
        assert job.payload["plan"]["consumption"]["quantity"] == "3"
        assert reply["world_effect"] is False and await chat.send(scope, message) == reply
        assert calls == [RoleTask.INTENT]
        assert repo.read_world(scope, world, branch).state.resources[_id(8)].balance == Decimal(stock)

        lease = repo.claim_job("consume-1", job_type=job.job_type)
        simulation = SimulationJobs(repo)
        first = simulation.advance(scope, lease)
        assert first.next_tick == due.due_tick
        boundary = repo.read_world(scope, world, branch)
        assert boundary.state.resources[_id(8)].balance == Decimal(stock)
        assert next(item for item in boundary.state.scheduled_events if item.id == due.id).status == "completed"

        if stock == "2":
            blocked = simulation.advance(scope, lease)
            assert blocked.interruption.reason_code == "insufficient_resource"
            assert repo.read_job(scope, job.job_id).status == JobStatus.PAUSED
            assert repo.read_world(scope, world, branch).state_hash == boundary.state_hash
            return

        with monkeypatch.context() as patch:
            patch.setattr(repo, "finish_job", interrupted_finish)
            with pytest.raises(SimulatedProcessLoss):
                simulation.advance(scope, lease)
        consumed = repo.read_world(scope, world, branch)
        assert consumed.state.current_tick == due.due_tick + 60
        assert consumed.state.resources[_id(8)].balance == 7
        assert consumed.state.resources[_id(9)].balance == 3

        repo.control_job(scope, job.job_id, "pause")
        repo.control_job(scope, job.job_id, "resume")
        renewed = repo.claim_job("consume-2", job_type=job.job_type)
        assert renewed and renewed.fencing_token > lease.fencing_token
        simulation.advance(scope, renewed)
        assert repo.read_job(scope, job.job_id).status == JobStatus.SUCCEEDED
        head = repo.read_world(scope, world, branch)
        assert head.state_hash == consumed.state_hash
        assert repo.replay(scope, world, branch, from_genesis=True).state_hash == head.state_hash
        assert await chat.send(scope, message) == reply and calls == [RoleTask.INTENT]
    finally:
        database.dispose()


def _request(created, actor_id, *, request_id=None):
    return MessageRequest(
        request_id=request_id or uuid4(),
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        session_id=uuid4(),
        actor_id=actor_id,
        expected_state_version=str(created.branch.state_version),
        mode="action",
        text="现在沿路线前往目的地。",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("activity", "outside_time", "boundary_tick"), [
    ("wait", False, 11), ("rest", False, 11), ("idle", False, 11),
    ("train", False, 11), ("train", True, 11),
    ("explore", False, 11), ("explore", True, 11), ("explore", False, 600),
])
async def test_stationary_chat_preserves_intent_and_elapsed_recovery_across_short_boundary(
    tmp_path, monkeypatch, activity, outside_time, boundary_tick,
):
    database, repo, scope, created, ids = _create_world(
        tmp_path, promise_boundary=True, promise_deadline=boundary_tick - 1, water_stock="2", injured=True,
    )
    try:
        actor, world, branch = ids["actor"], created.world.world_id, created.branch.branch_id
        calls = []
        skill_id = "skill.local_knowledge"
        duration = {"train": 59, "explore": 600}.get(activity, 7200)

        class Models:
            async def generate(self, scope, request):
                calls.append(request.role_task)
                action = {"kind": activity, "duration_ticks": duration}
                if activity == "train":
                    action["skill_id"] = skill_id
                elif activity == "explore":
                    action = {"kind": "explore", "place_id": str(ids["origin"])}
                return SimpleNamespace(normalized_output={"action": action, "clarification": ""})

        models = Models()
        actions = RulesActionDispatcher(repo, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
        chat = ConversationApplication(WorldApplication(repo, None), models,
                                       ConversationStore(OperationsStore(tmp_path / "ops.db")), actions=actions)
        text = {"rest": "现在休息两小时。", "train": "现在练习本地知识 59 秒。",
                "explore": "我现在搜查这个地方。"}.get(activity, "原地等两个小时。")
        message = _request(created, actor).model_copy(update={"text": text})

        def interrupted_finish(*args, **kwargs):
            raise SimulatedProcessLoss("Lost the acknowledgment after the job committed")

        with monkeypatch.context() as patch:
            patch.setattr(chat.store, "finish", interrupted_finish)
            with pytest.raises(SimulatedProcessLoss):
                await chat.send(scope, message)
        assert chat.store.recover_inflight() == 1
        reply = await chat.send(scope, message)
        assert calls == [RoleTask.INTENT] and reply["elapsed_ticks"] == "0"
        job_id = UUID(reply["job_id"])
        plan = repo.read_job(scope, job_id).payload["plan"]
        if activity == "explore":
            assert plan["exploration"]["place_id"] == str(ids["origin"])
        else:
            assert plan["stationary_only"] is True
        initial = repo.read_world(scope, world, branch)
        due, = initial.state.scheduled_events
        lease = repo.claim_job("stationary-window")
        assert lease and lease.job_id == job_id
        jobs = SimulationJobs(repo)
        assert jobs.advance(scope, lease).next_tick == due.due_tick == boundary_tick
        interrupted = repo.read_world(scope, world, branch)
        assert interrupted.state.entities[actor].components["health"]["current"] == 5
        assert interrupted.state.entities[actor].components["skills"]["skills"][skill_id]["experience"] == 0
        if outside_time:
            assert jobs.advance(scope, lease).next_tick == due.due_tick
        repo.control_job(scope, job_id, "pause")
        if outside_time:
            head = repo.read_world(scope, world, branch)
            command = CommandEnvelope(
                actor_id=actor, idempotency_key="independent-wait", expected_state_version=head.branch.state_version,
                command_type="action.wait", payload={"kind": "wait", "duration_ticks": 1},
            )
            ordinary_wait = RuleEngine().evaluate(head.state, command.payload, RuleContext(
                world_id=world, branch_id=branch, actor_id=actor, command_id=command.command_id,
                ruleset_version=head.world.ruleset_version,
            ))
            assert ordinary_wait.elapsed_ticks == 1
            repo.commit_command(scope, world, branch, command, ordinary_wait.events)
            with pytest.raises(Conflict, match="outside its simulation window"):
                repo.control_job(scope, job_id, "resume")
            assert repo.read_job(scope, job_id).status is JobStatus.PAUSED
            final = repo.read_world(scope, world, branch)
            trained = final.state.entities[actor].components["skills"]["skills"][skill_id]
            assert (trained["trained_ticks"], trained["experience"]) == ((11, 0) if activity == "train" else (0, 0))
            return
        repo.control_job(scope, job_id, "resume")
        lease = repo.claim_job("stationary-window-restored")
        assert lease and lease.job_id == job_id
        for _ in range(12):
            result = jobs.advance(scope, lease)
            job = repo.read_job(scope, job_id)
            if job.status is JobStatus.SUCCEEDED:
                break
            assert job.status is JobStatus.RUNNING, job.result
        else:
            pytest.fail("The requested station time did not finish")
        final = repo.read_world(scope, world, branch)
        assert final.state.current_tick == duration and final.state.entities[actor].location_id == ids["origin"]
        if activity == "explore":
            assert result.rule_resolution.outcome.code == "nothing_hidden_found"
        assert final.state.relationship_promises[0].status == "breached"
        assert final.state.resources[_id(8)].balance == 2
        assert final.state.entities[actor].components["health"]["current"] == (6 if activity == "rest" else 5)
        skill = final.state.entities[actor].components["skills"]["skills"][skill_id]
        assert (skill["trained_ticks"], skill["experience"]) == ((duration, 1) if activity == "train" else (0, 0))
        assert await chat.send(scope, message) == reply
        assert repo.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash
    finally:
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_tick", [0, 10])
async def test_chat_travel_settles_npc_training_at_its_own_boundary(tmp_path, start_tick):
    database, repo, scope, created, ids = _create_world(tmp_path, npc_training=True)
    try:
        actor, world, branch = ids["actor"], created.world.world_id, created.branch.branch_id
        learner_id, goal_id = _id(11), _id(12)
        skill_id, due_tick = "skill.local_knowledge", 10
        jobs = SimulationJobs(repo)
        setup = jobs.enqueue_world_advance(
            scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=due_tick),
            idempotency_key="start-npc-training", speed="balanced", depth="standard",
        )
        lease = repo.claim_job("npc-training-setup")
        assert lease and lease.job_id == setup.job_id
        pending, = jobs.advance(scope, lease).decision_requests
        snapshot = repo.read_world(scope, world, branch)
        plan = NpcPlan(
            plan_id=uuid5(pending.request_id, f"npc.plan.v1:{learner_id}:{pending.state_version}"),
            actor_id=learner_id, goal_ids=[goal_id], revision=1, status="ready", created_tick=0, updated_tick=0,
            next_review_tick=0, current_step=0, steps=[NpcPlanStep(capability_id="world.action.train.v1", arguments={
                "skill_id": skill_id, "duration_ticks": due_tick,
            })], reason="Practice local history",
        )
        jobs.advance(scope, lease, model_decision=DecisionResponse(
            request_id=pending.request_id, state_version=pending.state_version, at_tick=0,
            source="policy", option_id="plan", npc_planning_status="ready", npc_plan=plan,
            npc_plan_state_hash=snapshot.state_hash,
        ))
        jobs.advance(scope, lease)
        if start_tick == due_tick:
            assert jobs.advance(scope, lease).next_tick == due_tick
        repo.control_job(scope, setup.job_id, "cancel")
        before = repo.read_world(scope, world, branch)
        assert before.state.current_tick == start_tick and before.state.npc_plans[0].active_execution is not None

        class Models:
            async def generate(self, scope, request):
                assert request.role_task == RoleTask.INTENT
                return SimpleNamespace(normalized_output={
                    "action": {"kind": "move", "destination_id": str(ids["destination"])}, "clarification": "",
                })

        models = Models()
        rules = RuleEngine()
        actions = RulesActionDispatcher(repo, models, rules, ACTION_INTENT_ADAPTER, RuleContext)
        chat = ConversationApplication(WorldApplication(repo, None), models,
                                       ConversationStore(OperationsStore(tmp_path / "ops.db")), actions=actions)
        message = _request(before, actor)
        reply = await chat.send(scope, message)
        assert not reply["world_effect"]
        assert repo.read_world(scope, world, branch).state_hash == before.state_hash
        job_id = UUID(reply["job_id"])
        lease = repo.claim_job("travel-during-training")
        assert lease and lease.job_id == job_id
        for _ in range(12):
            jobs.advance(scope, lease)
            job = repo.read_job(scope, job_id)
            if job.status != JobStatus.RUNNING:
                break
        assert job.status == JobStatus.SUCCEEDED, job.result
        after = repo.read_world(scope, world, branch)
        assert after.state.current_tick == start_tick + 20
        assert after.state.entities[actor].location_id == ids["destination"]
        learner = after.state.entities[learner_id]
        assert learner.components["skills"]["skills"][skill_id]["last_practiced_tick"] == due_tick
        assert after.state.npc_plans[0].status == "completed"
        assert await chat.send(scope, message) == reply
        assert repo.replay(scope, world, branch, from_genesis=True).state_hash == after.state_hash
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_chat_reports_a_route_hazard_as_an_interruption_not_an_arrival(tmp_path):
    database, repo, scope, created, ids = _create_world(tmp_path, route_risk=Decimal("1"))
    try:
        actor, world, branch = ids["actor"], created.world.world_id, created.branch.branch_id
        before = repo.read_world(scope, world, branch)

        class Models:
            async def generate(self, scope, request):
                output = {"action": {"kind": "move", "destination_id": str(_id(3))}, "clarification": ""}
                return SimpleNamespace(normalized_output=output)

        models = Models()
        actions = RulesActionDispatcher(repo, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
        chat = ConversationApplication(WorldApplication(repo, None), models,
                                       ConversationStore(OperationsStore(tmp_path / "ops.db")), actions=actions)
        reply = await chat.send(scope, _request(created, actor))

        after = repo.read_world(scope, world, branch)
        assert reply["action_status"] == "partial"
        assert reply["world_effect"] is True
        assert "未抵达目的地" in reply["content"] and "10 世界秒" in reply["content"]
        assert after.state.current_tick == before.state.current_tick + 10
        assert after.state.entities[actor].location_id == before.state.entities[actor].location_id
        assert repo.replay(scope, world, branch, from_genesis=True).state_hash == after.state_hash
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_gm_recalls_direct_movement_only_on_branches_that_inherit_it(tmp_path):
    database, repo, scope, created, ids = _create_world(tmp_path)
    try:
        actor, world, branch = ids["actor"], created.world.world_id, created.branch.branch_id
        question_contexts = []

        class Models:
            async def generate(self, scope, request):
                if request.role_task == RoleTask.INTENT:
                    output = {"action": {"kind": "move", "destination_id": str(_id(3))}, "clarification": ""}
                else:
                    context = json.loads(request.messages[-1].content)["context"]
                    question_contexts.append(context)
                    output = {"answer": "按当前记录核对。", "references": [
                        move["event_id"] for move in context["own_recent_direct_moves"]
                    ]}
                return SimpleNamespace(normalized_output=output)

        models = Models()
        actions = RulesActionDispatcher(repo, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
        chat = ConversationApplication(WorldApplication(repo, None), models,
                                       ConversationStore(OperationsStore(tmp_path / "ops.db")), actions=actions)
        before = repo.fork(scope, world, branch, 0, "Before travel")
        assert (await chat.send(scope, _request(created, actor)))["world_effect"] is True
        head = repo.read_world(scope, world, branch)
        moved, = [event for event in repo.list_events(scope, world, branch).events
                  if event.type == "core.movement_completed.v1"]
        after = repo.fork(scope, world, branch, head.last_seq, "After travel")

        for fork, expected in ((before, []), (after, [{
            "event_id": str(moved.event_id), "started_tick": 0, "arrived_tick": 20,
            "from_place": "Departure Shore", "to_place": "Arrival Harbor",
        }])):
            message = _request(created, actor).model_copy(update={
                "branch_id": fork.branch_id, "mode": "ask_gm",
                "text": "刚才从哪里走到哪里？",
            })
            snapshot = repo.read_world(scope, world, fork.branch_id)
            reply = await chat.send(scope, message)
            assert not reply["world_effect"]
            assert reply["references"] == [move["event_id"] for move in expected]
            assert question_contexts[-1]["own_recent_direct_moves"] == expected
            assert repo.read_world(scope, world, fork.branch_id).state_hash == snapshot.state_hash
    finally:
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("route_ticks", "pause_tick", "reason"), [
    (20, 20, "window_elapsed"),
    (120, 60, "route_no_longer_fits_window"),
])
async def test_gm_distinguishes_a_paused_departure_from_a_journey(tmp_path, route_ticks, pause_tick, reason):
    database, repo, scope, created, ids = _create_world(
        tmp_path, route_specs=((4, route_ticks, ()),), water_stock="2", water_value=19,
    )
    try:
        actor, world, branch = ids["actor"], created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repo)
        job = jobs.enqueue(
            scope, world, branch,
            LongActionPlan(actor_id=actor, route_id=_id(4), started_tick=0, target_tick=route_ticks, activity="travel"),
            idempotency_key="travel-window", speed="balanced", depth="standard",
        )
        lease = repo.claim_job("travel-worker", job_type=job.job_type)
        assert lease and lease.job_id == job.job_id
        for _ in range(2):
            jobs.advance(scope, lease)
            paused = repo.read_job(scope, job.job_id)
            if paused.status != JobStatus.RUNNING:
                break
        assert paused.status == JobStatus.PAUSED
        original = repo.read_world(scope, world, branch)
        assert not original.state.travel_journeys
        fork = repo.fork(scope, world, branch, original.last_seq, "Same world facts")
        contexts = []

        class Models:
            async def generate(self, scope, request):
                assert request.role_task == RoleTask.GM_ANSWER
                context = json.loads(request.messages[-1].content)["context"]
                contexts.append(context)
                references = [row["job_id"] for row in context["own_recent_unstarted_travel"]]
                return SimpleNamespace(normalized_output={"answer": "按作业回执核对。", "references": references})

        models = Models()
        chat = ConversationApplication(WorldApplication(repo, None), models,
                                       ConversationStore(OperationsStore(tmp_path / "ops.db")))
        for branch_id, expected in ((branch, [{
            "job_id": str(job.job_id), "status": "paused_before_departure",
            "planned_start_tick": 0, "paused_tick": pause_tick, "reason": reason,
        }]), (fork.branch_id, [])):
            before = repo.read_world(scope, world, branch_id)
            message = _request(created, actor).model_copy(update={
                "branch_id": branch_id, "mode": "ask_gm",
                "expected_state_version": str(before.branch.state_version), "text": "我出发了吗？",
            })
            reply = await chat.send(scope, message)
            assert not reply["world_effect"]
            assert reply["references"] == ([str(job.job_id)] if expected else [])
            assert contexts[-1]["own_travel_job"] is None
            assert contexts[-1]["own_recent_unstarted_travel"] == expected
            assert repo.read_world(scope, world, branch_id).state_hash == before.state_hash
    finally:
        database.dispose()


@pytest.mark.parametrize("water_stock", [None, "2"])
def test_long_travel_queues_across_scheduled_promise_boundary(tmp_path, water_stock):
    database, repository, scope, created, ids = _create_world(
        tmp_path, promise_boundary=True, water_stock=water_stock,
    )
    try:
        view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        request = _request(created, ids["actor"])
        intent = MoveIntent(destination_id=ids["destination"])
        rules = RuleEngine()
        context = RuleContext(
            world_id=created.world.world_id,
            branch_id=created.branch.branch_id,
            command_id=request.request_id,
            actor_id=ids["actor"],
            ruleset_version=created.world.ruleset_version,
        )
        rejected_short_move = rules.evaluate(view.state, intent, context)
        assert rejected_short_move.outcome.code == "scheduled_boundary_required"
        assert rejected_short_move.outcome.status == "rejected"

        jobs = SimulationJobs(repository)
        reply = enqueue_travel_for_move(scope, view, request, intent, jobs, rules)
        job_id = UUID(reply["job_id"])
        job = repository.read_job(scope, job_id)
        assert job.payload["plan"]["activity"] == "travel"
        assert job.payload["plan"]["started_tick"] == 0
        assert job.payload["plan"]["target_tick"] == 20
        assert job.payload["plan"]["depart_immediately"] is True
        assert reply["elapsed_ticks"] == "0" and reply["world_effect"] is False
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state.current_tick == 0

        lease = repository.claim_job("travel-chat-test", job_type="simulation.long_action")
        assert lease is not None and lease.job_id == job_id
        observations = []
        for _ in range(20):
            jobs.advance(scope, lease)
            snapshot = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
            promise = snapshot.state.relationship_promises[0]
            observations.append((snapshot.state.current_tick, promise.status))
            current_job = repository.read_job(scope, job_id)
            if current_job.status != JobStatus.RUNNING:
                break

        assert current_job.status == JobStatus.SUCCEEDED
        final = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert final.state.current_tick == 20
        assert final.state.entities[ids["actor"]].location_id == ids["destination"]
        assert final.state.relationship_promises[0].status == "breached"
        assert final.state.scheduled_events[0].status == "completed"
        journey, = final.state.travel_journeys
        assert (journey.departed_tick, journey.resolved_tick) == (0, 20)
        if water_stock is not None:
            assert final.state.resources[_id(8)].balance == Decimal(water_stock)
        assert (11, "active") in observations
        assert (11, "breached") in observations
        assert observations.index((11, "breached")) < observations.index((20, "breached"))
    finally:
        database.dispose()


def test_retry_reuses_account_request_job_and_shortest_route_tiebreak(tmp_path):
    # The ordinary move rule sorts by duration, then the route UUID string.
    database, repository, scope, created, ids = _create_world(
        tmp_path,
        route_specs=((9, 30, ()), (5, 20, ()), (4, 20, ())),
    )
    try:
        view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        request = _request(created, ids["actor"], request_id=_id(100))
        jobs = SimulationJobs(repository)
        rules = RuleEngine()
        first = enqueue_travel_for_move(
            scope, view, request, MoveIntent(destination_id=ids["destination"]), jobs, rules,
        )
        second = enqueue_travel_for_move(
            scope, view, request, MoveIntent(destination_id=ids["destination"]), jobs, rules,
        )

        assert first == second
        assert first["references"] == [str(_id(4))]
        assert len(repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)) == 1
        job = repository.find_job_by_key(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            travel_message_key(scope, request.request_id),
        )
        assert job is not None
        assert job.payload["plan"]["route_id"] == str(_id(4))
        assert first == travel_queue_reply(request.request_id, job)
        assert travel_message_key(scope, request.request_id) == (
            f"travel-message:{scope.user_id}:{request.request_id}"
        )
    finally:
        database.dispose()


def test_rejected_route_access_never_enqueues_travel_job(tmp_path):
    database, repository, scope, created, ids = _create_world(
        tmp_path,
        route_specs=((4, 20, ("route.pass",)),),
    )
    try:
        view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        request = _request(created, ids["actor"])
        reply = enqueue_travel_for_move(
            scope,
            view,
            request,
            MoveIntent(destination_id=ids["destination"]),
            SimulationJobs(repository),
            RuleEngine(),
        )

        assert reply["action_status"] == "rejected"
        assert "通行条件" in reply["content"]
        assert repository.list_jobs(scope, created.world.world_id, created.branch.branch_id) == []
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state.current_tick == 0
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_action_chat_recovers_queued_travel_after_transcript_failure(tmp_path, monkeypatch):
    database, repository, scope, created, ids = _create_world(tmp_path, promise_boundary=True)
    store = ConversationStore(OperationsStore(tmp_path / "operations.db"))
    calls = []

    class Models:
        async def generate(self, identity, model_request):
            assert identity == scope
            calls.append(model_request.role_task)
            assert model_request.role_task == RoleTask.INTENT
            output = {
                "action": {"kind": "move", "destination_id": str(ids["destination"])},
                "clarification": "",
            }
            Draft202012Validator(model_request.output_schema).validate(output)
            return SimpleNamespace(normalized_output=output)

    try:
        models = Models()
        rules = RuleEngine()
        app = ConversationApplication(
            WorldApplication(repository, None),
            models,
            store,
            actions=RulesActionDispatcher(
                repository, models, rules, ACTION_INTENT_ADAPTER, RuleContext,
            ),
        )
        request = _request(created, ids["actor"])
        original_finish = store.finish
        captured = {}

        def lose_response(_scope, _request_id, result, *, succeeded):
            assert succeeded is True
            captured.update(result)
            raise SimulatedProcessLoss

        monkeypatch.setattr(store, "finish", lose_response)
        with pytest.raises(SimulatedProcessLoss):
            await app.send(scope, request)
        monkeypatch.setattr(store, "finish", original_finish)

        job, = repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)
        assert job.status == JobStatus.QUEUED
        assert job.payload["plan"]["activity"] == "travel"
        assert job.payload["plan"]["target_tick"] == 20
        assert UUID(captured["job_id"]) == job.job_id
        assert captured["world_effect"] is False and captured["elapsed_ticks"] == "0"
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state.current_tick == 0

        assert store.recover_inflight() == 1
        repository.control_job(scope, job.job_id, "cancel")
        recovered_status = app.status(scope, request.request_id)
        assert recovered_status == {"status": "succeeded", "result": captured}
        assert await app.send(scope, request) == captured
        assert calls == [RoleTask.INTENT]
        assert len(repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)) == 1
        assert repository.read_job(scope, job.job_id).status == JobStatus.CANCELLED
    finally:
        database.dispose()
