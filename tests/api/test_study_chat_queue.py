import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import NeedValue, NeedsComponent, SkillValue, SkillsComponent
from world_engine.domain.errors import Conflict
from world_engine.domain.models import AccessScope, CalendarConfig, JobStatus, NewJob, NewWorld, WorldEntity, WorldState
from world_engine.rules import ACTION_INTENT_ADAPTER, RuleContext, RuleEngine
from world_engine.simulation import LongActionPlan, SimulationEngine
from world_engine.simulation.engine import SimulationPolicy
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


class SimulatedProcessLoss(BaseException):
    """Interrupt after the job commits and before the transcript is completed."""


@pytest.mark.asyncio
async def test_gm_reads_training_and_rest_as_distinct_committed_actions(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    actor_id = uuid4()
    skill_id = "skill.local_knowledge"
    database = Database(f"sqlite:///{(tmp_path / 'training.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    contexts = []

    class Models:
        async def generate(self, _scope, request):
            last = request.messages[-1].content
            if request.role_task is RoleTask.GM_ANSWER:
                context = json.loads(last)["context"]
                contexts.append(context)
                output = {"answer": "按已结算练习核对。", "references": [
                    row["event_id"] for row in context["own_completed_training"]
                ]}
            else:
                assert request.role_task is RoleTask.INTENT
                action = {"kind": "rest", "duration_ticks": 1800}
                if "练习" in last:
                    action.update(kind="train", skill_id=skill_id)
                output = {"action": action, "clarification": ""}
            return SimpleNamespace(normalized_output=output)

    try:
        actor = WorldEntity(id=actor_id, kind="character", name="Learner", components={
            "skills": SkillsComponent(skills={skill_id: SkillValue(level=12)}).model_dump(mode="json"),
        })
        created = repository.create_world(scope, NewWorld(title="Training", state=WorldState(
            player_id=actor_id, entities={actor_id: actor},
        )))
        world, branch = created.world.world_id, created.branch.branch_id
        models = Models()
        app = ConversationApplication(
            WorldApplication(repository, None), models, ConversationStore(OperationsStore(tmp_path / "ops.db")),
            actions=RulesActionDispatcher(repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext),
        )

        async def send(branch_id, mode, text):
            before = repository.read_world(scope, world, branch_id)
            return await app.send(scope, MessageRequest(
                request_id=uuid4(), world_id=world, branch_id=branch_id, session_id=branch_id,
                actor_id=actor_id, expected_state_version=str(before.branch.state_version), mode=mode, text=text,
            ))

        rest = await send(branch, "action", "休息半小时。")
        assert rest["world_effect"]
        rested = repository.read_world(scope, world, branch)
        fork = repository.fork(scope, world, branch, rested.last_seq, "Before training")
        trained = await send(branch, "action", "练习本地知识半小时。")
        assert trained["world_effect"]
        trained_head = repository.read_world(scope, world, branch)
        assert trained_head.state.current_tick == 3600
        answer = await send(branch, "ask_gm", "我刚才练习了多久？")
        receipt, = contexts[-1]["own_completed_training"]
        assert receipt == {"event_id": answer["references"][0], "skill_id": skill_id,
                           "started_tick": 1800, "ended_tick": 3600, "duration_seconds": 1800}
        rest_receipt, = contexts[-1]["own_completed_rest"]
        assert rest_receipt["ended_tick"] == 1800 and rest_receipt["event_id"] != receipt["event_id"]
        assert not answer["world_effect"]
        assert contexts[-1]["own_study_totals"] == []
        assert repository.read_world(scope, world, branch).state_hash == trained_head.state_hash
        fork_answer = await send(fork.branch_id, "ask_gm", "我刚才练习了多久？")
        assert fork_answer["references"] == []
        assert contexts[-1]["own_completed_training"] == []
        assert contexts[-1]["own_completed_rest"] == [rest_receipt]

        jobs = SimulationJobs(repository, SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=900)))
        for activity, start, end in [("wait", 3600, 4200), ("study", 4200, 12300)]:
            head = repository.read_world(scope, world, branch)
            job = jobs.enqueue(scope, world, branch, LongActionPlan(
                actor_id=actor_id, activity=activity, started_tick=start, target_tick=end,
                skill_id=skill_id if activity == "study" else None,
            ), idempotency_key=activity, speed="balanced", depth="standard",
                expected_state_version=head.branch.state_version)
            lease = repository.claim_job("study-reader", job_type=job.job_type)
            assert lease and lease.job_id == job.job_id
            for index in range(9):
                jobs.advance(scope, lease)
                job = repository.read_job(scope, job.job_id)
                if job.status != JobStatus.RUNNING:
                    break
                if activity == "study" and index == 0:
                    paused = repository.control_job(scope, job.job_id, "pause")
                    assert paused.status == JobStatus.PAUSED
                    partial = repository.read_world(scope, world, branch)
                    totals = repository.list_actor_study_totals(
                        scope, world, branch, actor_id, through_seq=partial.last_seq,
                    )
                    assert totals[0]["actual_training_seconds"] == 900
                    assert (await send(branch, "action", "休息半小时。"))["world_effect"]
                    queued = repository.control_job(scope, job.job_id, "resume")
                    assert queued.committed_through_tick == 6900
                    assert queued.payload["plan"]["target_tick"] == 12300
                    lease = repository.claim_job("study-reader-resumed", job_type=job.job_type)
                    assert lease and lease.job_id == job.job_id
            assert job.status == JobStatus.SUCCEEDED
        after_job = repository.read_world(scope, world, branch)
        answer = await send(branch, "ask_gm", "这趟学习实际在哪几段练习过？")
        history = contexts[-1]["own_completed_training"]
        assert [(row["started_tick"], row["ended_tick"], row["duration_seconds"]) for row in history] == [
            (start, start + 900, 900) for start in range(6900, 12300, 900)
        ]
        assert contexts[-1]["own_study_totals"] == [{
            "job_id": str(job.job_id), "skill_id": skill_id,
            "planned_start_tick": 4200, "planned_target_tick": 12300,
            "actual_training_seconds": 6300, "committed_slice_count": 7,
        }]
        assert all(row["skill_id"] == skill_id for row in history)
        assert answer["references"] == [row["event_id"] for row in history]
        assert repository.read_world(scope, world, branch).state_hash == after_job.state_hash

        backup = BackupApplication(repository, scratch_directory=tmp_path / "study-backup-scratch")
        archive = backup.validate_archive(backup.export_world(scope, world, format_version="2.0"))
        restored_db = Database(f"sqlite:///{(tmp_path / 'study-restored.db').as_posix()}")
        try:
            restored_db.create_schema()
            restored_repo = WorldRepository(restored_db)
            WorldBackupImporter(restored_repo).import_verified_backup(scope, archive)
            assert restored_repo.list_jobs(scope, world, branch) == []
            assert restored_repo.list_actor_training_history(scope, world, branch, actor_id,
                                                            through_seq=after_job.last_seq) == history
            assert restored_repo.list_actor_study_totals(scope, world, branch, actor_id,
                                                        through_seq=after_job.last_seq) == []
        finally:
            restored_db.dispose()
        expired = jobs.enqueue(scope, world, branch, LongActionPlan(
            actor_id=actor_id, activity="study", started_tick=12300, target_tick=12900, skill_id=skill_id,
        ), idempotency_key="expired-study", speed="balanced", depth="standard",
            expected_state_version=after_job.branch.state_version)
        repository.control_job(scope, expired.job_id, "pause")
        assert (await send(branch, "action", "休息半小时。"))["world_effect"]
        with pytest.raises(Conflict, match="outside its original time window"):
            repository.control_job(scope, expired.job_id, "resume")
        assert repository.read_job(scope, expired.job_id).status == JobStatus.PAUSED
    finally:
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [None, 40])
async def test_study_chat_uses_world_calendar_and_recovers_the_same_job(tmp_path, monkeypatch, threshold):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    actor_id, place_id = uuid4(), uuid4()
    skill_id = "skill.cultivation_practice"
    calendar = CalendarConfig(day_seconds=80_000, year_days=360)
    actor = WorldEntity(id=actor_id, kind="character", name="Learner", location_id=place_id, components={
        "skills": SkillsComponent(skills={skill_id: SkillValue(level=32)}).model_dump(mode="json"),
        "needs": NeedsComponent(safety=NeedValue(value=69)).model_dump(mode="json"),
    })
    database = Database(f"sqlite:///{(tmp_path / 'study.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    store = ConversationStore(OperationsStore(tmp_path / "operations.db"))
    generation_roles = []
    study_contexts = []

    class Models:
        compiled_safety = threshold

        async def generate(self, identity, model_request):
            assert identity == scope
            generation_roles.append(model_request.role_task)
            if model_request.role_task == RoleTask.GM_ANSWER:
                context = json.loads(model_request.messages[-1].content)["context"]
                study_contexts.append(context)
                output = {"answer": "你可以比较计划时长和实际练习时间。", "references": [
                    row["job_id"] for row in context["own_study_jobs"]
                ]}
            else:
                assert model_request.role_task == RoleTask.INTENT
                output = {
                    "action": {"kind": "queue_study", "skill_id": skill_id, "amount": 2, "unit": "year"},
                    "clarification": "",
                }
                if self.compiled_safety is not None:
                    output["action"]["study_stop_safety_at_or_below"] = self.compiled_safety
            Draft202012Validator(model_request.output_schema).validate(output)
            return SimpleNamespace(normalized_output=output)

    try:
        created = repository.create_world(scope, NewWorld(title="Study fixture", state=WorldState(
            calendar=calendar, player_id=actor_id,
            entities={actor_id: actor, place_id: WorldEntity(id=place_id, kind="place", name="Study hall")},
        )))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        models = Models()
        app = ConversationApplication(WorldApplication(repository, None), models, store, actions=RulesActionDispatcher(
            repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
        ))
        instruction = "现在开始继续练习我已有的功法两年。"
        if threshold is not None:
            instruction += f"安全值不高于 {threshold} 时暂停。"
        request = MessageRequest(request_id=uuid4(), world_id=world_id, branch_id=branch_id, session_id=uuid4(),
            actor_id=actor_id, expected_state_version="0", mode="action", text=instruction)
        question = request.model_copy(update={
            "request_id": uuid4(), "mode": "ask_gm", "text": "如果我练习两年，会发生什么？",
        })
        answer = await app.send(scope, question)
        assert answer["kind"] == "query" and not answer["world_effect"]
        assert study_contexts[-1]["own_study_jobs"] == []
        assert repository.list_jobs(scope, world_id, branch_id) == []
        if threshold is not None:
            for wrong_value in (None, threshold + 1):
                models.compiled_safety = wrong_value
                changed = await app.send(scope, request.model_copy(update={"request_id": uuid4()}))
                assert changed["kind"] == "clarification" and not changed["world_effect"]
                assert repository.list_jobs(scope, world_id, branch_id) == []
            models.compiled_safety = threshold
            unwritten = await app.send(scope, request.model_copy(update={
                "request_id": uuid4(), "text": "现在继续练习功法两年。",
            }))
            assert unwritten["kind"] == "clarification" and not unwritten["world_effect"]
            assert repository.list_jobs(scope, world_id, branch_id) == []
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
        job, = repository.list_jobs(scope, world_id, branch_id)
        assert job.status == JobStatus.QUEUED
        assert job.payload["plan"]["target_tick"] == 2 * calendar.year_days * calendar.day_seconds
        assert job.payload["plan"]["skill_id"] == skill_id
        if threshold is not None:
            condition, = job.payload["plan"]["stop_conditions"]
            assert (condition["subject_id"], condition["need"], condition["threshold"]) == (str(actor_id), "safety", threshold)
            assert f"达到或低于 {threshold} 时暂停" in captured["content"]
        else:
            assert job.payload["plan"]["stop_conditions"] == []
        assert UUID(captured["job_id"]) == job.job_id and not captured["world_effect"]
        after_queue = repository.read_world(scope, world_id, branch_id)
        assert after_queue.last_seq == 0 and after_queue.state.current_tick == 0

        assert store.recover_inflight() == 1
        repository.control_job(scope, job.job_id, "cancel")
        assert app.status(scope, request.request_id) == {"status": "succeeded", "result": captured}
        assert await app.send(scope, request) == captured
        assert len(repository.list_jobs(scope, world_id, branch_id)) == 1
        readback = await app.send(scope, question.model_copy(update={"request_id": uuid4(), "text": "这项闭关现在是什么状态？"}))
        assert readback["references"] == [str(job.job_id)] and not readback["world_effect"]
        receipt, = study_contexts[-1]["own_study_jobs"]
        assert (receipt["status"], receipt["skill_id"], receipt["target_tick"]) == (
            "cancelled", skill_id, str(2 * calendar.year_days * calendar.day_seconds),
        )
        assert receipt.get("study_stop_safety_at_or_below") == threshold
        assert repository.read_world(scope, world_id, branch_id).state_hash == after_queue.state_hash
        assert generation_roles == (
            [RoleTask.GM_ANSWER] + [RoleTask.INTENT] * (4 if threshold is not None else 1) + [RoleTask.GM_ANSWER]
        )

        legacy_branch = repository.fork(scope, world_id, branch_id, 0, "Legacy study")
        legacy_plan = LongActionPlan(actor_id=actor_id, activity="study", skill_id=skill_id, started_tick=0, target_tick=120)
        legacy_job = repository.enqueue_job(scope, NewJob(
            world_id=world_id, branch_id=legacy_branch.branch_id, job_type="simulation.long_action",
            idempotency_key="legacy-study",
            payload={"plan": legacy_plan.model_dump(mode="json"), "speed": "fast", "depth": "standard",
                     "initial_state_version": 0},
        ))
        recovered = SimulationJobs(repository).enqueue(
            scope, world_id, legacy_branch.branch_id, legacy_plan, idempotency_key="legacy-study",
            speed="fast", depth="standard", expected_state_version=0,
        )
        assert recovered.job_id == legacy_job.job_id and "submitted_by" not in recovered.payload
    finally:
        database.dispose()
