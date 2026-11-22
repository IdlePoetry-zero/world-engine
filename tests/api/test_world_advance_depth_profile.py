from itertools import product
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import DepthProfile, SpeedProfile, execution_profile
import world_engine.application.simulation_jobs as simulation_jobs_module
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.models import (
    AccessScope,
    NewJob,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.simulation.contracts import LongActionPlan, SimulationContext, WorldAdvancePlan
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _database(tmp_path, name="world-advance-depth.db"):
    database = Database(f"sqlite:///{(tmp_path / name).as_posix()}")
    database.create_schema()
    return database


def test_default_context_serialization_omits_optional_profile_cap():
    context = SimulationContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(),
        world_seed="context-serialization", state_version=0,
    )

    assert "max_steady_macro_days" not in context.model_dump(mode="json")
    assert context.model_copy(update={"max_steady_macro_days": 7}).model_dump(mode="json")[
        "max_steady_macro_days"
    ] == 7


@pytest.mark.parametrize(
    ("speed", "depth"),
    list(product(tuple(item.value for item in SpeedProfile), tuple(item.value for item in DepthProfile))),
)
def test_new_world_advance_pins_profile_cap_for_all_speed_and_depth_pairs(tmp_path, speed, depth):
    database = _database(tmp_path)
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        world = repository.create_world(scope, NewWorld(title="Depth cap", state=WorldState()))
        job = SimulationJobs(repository).enqueue_world_advance(
            scope,
            world.world.world_id,
            world.branch.branch_id,
            WorldAdvancePlan(started_tick=0, target_tick=10_000_000),
            idempotency_key=f"depth-cap-{speed}-{depth}",
            speed=speed,
            depth=depth,
            expected_state_version=0,
        )

        profile = execution_profile(SpeedProfile(speed), DepthProfile(depth))
        assert job.payload["speed"] == speed
        assert job.payload["depth"] == depth
        assert job.payload["max_steady_macro_days"] == profile.max_steady_macro_days
        assert job.payload["npc_reflection_mode"] == profile.reflection_mode
        assert repository.read_job(scope, job.job_id).payload == job.payload
    finally:
        database.dispose()


def test_new_profile_cap_uses_short_calendar_days_and_stops_exactly_at_target(tmp_path):
    database = _database(tmp_path)
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        world = repository.create_world(scope, NewWorld(
            title="Short calendar horizon",
            state=WorldState(calendar={"day_seconds": 10, "year_days": 360}),
        ))
        jobs = SimulationJobs(repository)
        queued = jobs.enqueue_world_advance(
            scope,
            world.world.world_id,
            world.branch.branch_id,
            WorldAdvancePlan(started_tick=0, target_tick=100),
            idempotency_key="short-calendar-cap-and-target",
            speed="patient",
            depth="exhaustive",
            expected_state_version=0,
        )
        assert queued.payload["max_steady_macro_days"] == 7
        lease = repository.claim_job("short-calendar-worker", job_type="simulation.world_advance")
        assert lease is not None and lease.job_id == queued.job_id

        first = jobs.advance(scope, lease)
        assert first.outcome == "advanced"
        assert first.next_tick == 70
        assert repository.read_world(scope, world.world.world_id, world.branch.branch_id).state.current_tick == 70

        second = jobs.advance(scope, lease)
        assert second.outcome == "completed"
        assert second.next_tick == 100
        assert repository.read_world(scope, world.world.world_id, world.branch.branch_id).state.current_tick == 100
    finally:
        database.dispose()


def test_short_custom_day_scheduled_event_is_an_exact_steady_horizon_boundary(tmp_path):
    database = _database(tmp_path)
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    scheduled_id = uuid4()
    try:
        world = repository.create_world(scope, NewWorld(
            title="Short scheduled boundary",
            state=WorldState(
                calendar={"day_seconds": 10, "year_days": 360},
                scheduled_events=[ScheduledEventRecord(
                    id=scheduled_id,
                    due_tick=25,
                    phase="environment",
                    handler_id="test.unhandled_event",
                    handler_version="1.0.0",
                )],
            ),
        ))
        jobs = SimulationJobs(repository)
        queued = jobs.enqueue_world_advance(
            scope,
            world.world.world_id,
            world.branch.branch_id,
            WorldAdvancePlan(started_tick=0, target_tick=100),
            idempotency_key="scheduled-boundary-with-short-day",
            speed="fast",
            depth="exhaustive",
            expected_state_version=0,
        )
        lease = repository.claim_job("scheduled-boundary-worker", job_type="simulation.world_advance")
        assert lease is not None and lease.job_id == queued.job_id

        result = jobs.advance(scope, lease)
        assert result.outcome == "advanced"
        assert result.next_tick == 25
        assert repository.read_world(scope, world.world.world_id, world.branch.branch_id).state.current_tick == 25
        saved_events = repository.list_events(scope, world.world.world_id, world.branch.branch_id).events
        assert saved_events[-1].tick == 25
    finally:
        database.dispose()


def test_legacy_untagged_world_advance_retry_keeps_payload_and_default_horizon(tmp_path):
    database = _database(tmp_path)
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        world = repository.create_world(scope, NewWorld(
            title="Legacy horizon",
            state=WorldState(calendar={"day_seconds": 10, "year_days": 360}),
        ))
        plan = WorldAdvancePlan(started_tick=0, target_tick=500)
        old_payload = {
            "plan": plan.model_dump(mode="json"),
            "speed": "balanced",
            "depth": "deep",
            "initial_state_version": 0,
        }
        old_job = repository.enqueue_job(scope, NewJob(
            world_id=world.world.world_id,
            branch_id=world.branch.branch_id,
            job_type="simulation.world_advance",
            idempotency_key="pre-profile-world-advance",
            payload=old_payload,
        ), expected_state_version=0)

        retried = SimulationJobs(repository).enqueue_world_advance(
            scope,
            world.world.world_id,
            world.branch.branch_id,
            plan,
            idempotency_key="pre-profile-world-advance",
            speed="balanced",
            depth="deep",
            expected_state_version=0,
        )
        assert retried.job_id == old_job.job_id
        assert retried.payload == old_payload
        assert "max_steady_macro_days" not in retried.payload
        assert "npc_reflection_mode" not in retried.payload

        lease = repository.claim_job("legacy-horizon-worker", job_type="simulation.world_advance")
        assert lease is not None and lease.job_id == old_job.job_id
        result = SimulationJobs(repository).advance(scope, lease)
        assert result.outcome == "advanced"
        assert result.next_tick == 300
    finally:
        database.dispose()


def test_tagged_world_advance_retry_reuses_pinned_cap_after_profile_changes(tmp_path, monkeypatch):
    database = _database(tmp_path)
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        world = repository.create_world(scope, NewWorld(title="Pinned depth cap", state=WorldState()))
        jobs = SimulationJobs(repository)
        plan = WorldAdvancePlan(started_tick=0, target_tick=10_000)
        first = jobs.enqueue_world_advance(
            scope,
            world.world.world_id,
            world.branch.branch_id,
            plan,
            idempotency_key="tagged-world-advance-retry",
            speed="balanced",
            depth="exhaustive",
            expected_state_version=0,
        )
        assert first.payload["max_steady_macro_days"] == 7
        assert first.payload["npc_reflection_mode"] == "salient_events_plans_and_conflicts"
        monkeypatch.setattr(
            simulation_jobs_module,
            "execution_profile",
            lambda *_args: SimpleNamespace(max_steady_macro_days=29),
        )

        retried = jobs.enqueue_world_advance(
            scope,
            world.world.world_id,
            world.branch.branch_id,
            plan,
            idempotency_key="tagged-world-advance-retry",
            speed="balanced",
            depth="exhaustive",
            expected_state_version=0,
        )
        assert retried.job_id == first.job_id
        assert retried.payload == first.payload
        assert retried.payload["max_steady_macro_days"] == 7
        assert retried.payload["npc_reflection_mode"] == first.payload["npc_reflection_mode"]
    finally:
        database.dispose()


def test_new_long_action_payload_does_not_receive_world_advance_cap(tmp_path):
    database = _database(tmp_path)
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    actor_id = uuid4()
    try:
        world = repository.create_world(scope, NewWorld(
            title="Long action profile isolation",
            state=WorldState(
                player_id=actor_id,
                entities={actor_id: WorldEntity(id=actor_id, kind="character", name="Traveler")},
            ),
        ))
        job = SimulationJobs(repository).enqueue(
            scope,
            world.world.world_id,
            world.branch.branch_id,
            LongActionPlan(actor_id=actor_id, started_tick=0, target_tick=100, activity="wait"),
            idempotency_key="long-action-no-world-cap",
            speed="balanced",
            depth="deep",
            expected_state_version=0,
        )
        assert "max_steady_macro_days" not in job.payload
    finally:
        database.dispose()
