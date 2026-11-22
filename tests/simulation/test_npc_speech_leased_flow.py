"""SQLite leased-flow coverage for private NPC speech in a complete modern city.

The fixed ``source='policy'`` responses below are deterministic test decisions,
not real model/LLM behavior.  The world owner is the test account, the catalog's
``player_id`` is the player character, and a second test membership takes over
the NPC only after a later plan has been accepted.
"""

from collections import Counter
from datetime import datetime, timezone
import json
from uuid import uuid4, uuid5

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.visibility import project_event, project_player_state
from world_engine.content import ContentCatalog
from world_engine.domain.components import KnowledgeRecord
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.models import (
    AccessScope,
    MembershipRole,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.reducers import state_hash
from world_engine.rules.contracts import RuleContext
from world_engine.storage import repository as repository_module
from world_engine.simulation import WorldAdvancePlan
from world_engine.simulation.contracts import DecisionResponse
from world_engine.simulation.npc_capabilities import (
    capability_id_for_action,
    preflight_npc_plan_step,
)
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


_FIRST_UTTERANCE = "leased-flow private greeting: first visit"
_TAKEOVER_UTTERANCE = "leased-flow private greeting: after account takeover"
_REVIEW_INTERVAL = 60


def _active_people(state: WorldState) -> list[WorldEntity]:
    return [
        entity for entity in state.entities.values()
        if entity.kind in {"character", "person"}
        and entity.status == "active"
        and entity.id != state.player_id
        and entity.location_id is not None
    ]


def _speaker_and_recipient(draft, state: WorldState) -> tuple[WorldEntity, WorldEntity]:
    goals = {
        goal.owner_id
        for goal in state.goals
        if goal.status == "active"
        and goal.created_tick <= state.current_tick
        and (goal.deadline is None or goal.deadline >= state.current_tick)
    }
    people = sorted(_active_people(state), key=lambda item: str(item.id))
    step = NpcPlanStep(
        capability_id=capability_id_for_action("speak"),
        arguments={"target_ids": [], "utterance": _FIRST_UTTERANCE},
    )
    for actor in people:
        if actor.id not in goals:
            continue
        for target in people:
            if target.id == actor.id or target.location_id != actor.location_id:
                continue
            preflight = preflight_npc_plan_step(
                state,
                actor.id,
                step.model_copy(update={"arguments": {
                    "target_ids": [str(target.id)], "utterance": _FIRST_UTTERANCE,
                }}),
                RuleContext(
                    world_id=draft.world_id,
                    branch_id=draft.branch_id,
                    command_id=uuid4(),
                    actor_id=actor.id,
                    ruleset_version=draft.ruleset_version,
                ),
            )
            if preflight.status == "instant" and preflight.proposed_events:
                return actor, target
    raise AssertionError("The natural modern_city seed must contain an eligible co-located NPC pair.")


def _test_plan(request, state: WorldState, actor_id, recipient_id, draft, utterance: str) -> DecisionResponse:
    goal = min(
        (
            item for item in state.goals
            if item.owner_id == actor_id
            and item.status == "active"
            and item.created_tick <= state.current_tick
            and (item.deadline is None or item.deadline >= state.current_tick)
        ),
        key=lambda item: str(item.id),
    )
    plan = NpcPlan(
        plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor_id}:{request.state_version}"),
        actor_id=actor_id,
        goal_ids=[goal.id],
        revision=1,
        status="ready",
        created_tick=state.current_tick,
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick,
        current_step=0,
        steps=[NpcPlanStep(
            capability_id=capability_id_for_action("speak"),
            arguments={"target_ids": [str(recipient_id)], "utterance": utterance},
        )],
        reason="Fixed policy fixture selects a private greeting for the leased-flow test.",
    )
    return DecisionResponse(
        request_id=request.request_id,
        state_version=request.state_version,
        at_tick=request.at_tick,
        source="policy",
        option_id="plan",
        explanation="Deterministic test policy; no model call was made.",
        npc_planning_status="ready",
        npc_plan=plan,
        npc_plan_state_hash=state_hash(state),
    )


def _private_knowledge_for_claim(state: WorldState, text: str) -> list[KnowledgeRecord]:
    return [record for record in state.knowledge if text in record.claim]


def test_sqlite_leased_npc_speech_is_private_zero_time_and_takeover_blocks_followup(tmp_path, monkeypatch):
    # Keep the worker's lease live while the test inspects and replays committed events.
    lease_clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(repository_module, "utc_now", lambda: lease_clock)
    catalog = ContentCatalog()
    draft = catalog.build_world(
        "modern_city", "Full natural modern city leased speech", "Catalog Player", seed=2718,
    )
    source_state = draft.state
    source_shape = Counter(entity.kind for entity in source_state.entities.values())
    assert source_shape["polity"] == 24
    assert source_shape["organization"] == 24
    assert source_shape["character"] == 273
    assert source_shape["place"] == 120
    assert len(source_state.goals) == 321
    assert source_state.current_tick == 0

    speaker, recipient = _speaker_and_recipient(draft, source_state)
    player_id = source_state.player_id
    assert player_id is not None and player_id not in {speaker.id, recipient.id}
    schedule = build_npc_plan_review_schedule(
        source_state,
        draft.world_id,
        speaker.id,
        source_state.current_tick,
        repeat_ticks=_REVIEW_INTERVAL,
        max_runs=2,
    )
    authored_state = source_state.model_copy(update={
        "scheduled_events": [schedule, *source_state.scheduled_events],
    })
    # The single permitted author setup adds only a bounded goal-review schedule.
    assert authored_state.entities == source_state.entities
    assert authored_state.goals == source_state.goals
    assert authored_state.resources == source_state.resources
    assert len(authored_state.entities) == len(source_state.entities)
    world_draft = draft.model_copy(update={"state": authored_state})

    database = Database(f"sqlite:///{(tmp_path / 'npc-speech-leased.db').as_posix()}")
    database.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    takeover_user_id = uuid4()
    try:
        repository = WorldRepository(database)
        world = repository.create_world(scope, world_draft)
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        takeover_state = repository.read_world(scope, world_id, branch_id)
        assert takeover_state.state == authored_state
        repository.grant_membership(
            scope, world_id, takeover_user_id, MembershipRole.PLAYER,
        )

        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(started_tick=0, target_tick=120),
            idempotency_key="leased-private-speech-full-world",
            speed="balanced",
            depth="standard",
        )
        lease = repository.claim_job("npc-speech-policy-worker", job_type=job.job_type)
        assert lease is not None and lease.job_id == job.job_id

        pending = jobs.advance(scope, lease)
        assert pending.outcome == "decision_required"
        first_request, = pending.decision_requests
        assert first_request.kind == "npc.plan_review"
        assert first_request.actor_id == speaker.id
        before_first_review = repository.read_world(scope, world_id, branch_id)
        first_response = _test_plan(
            first_request, before_first_review.state, speaker.id, recipient.id,
            world.world.ruleset_version, _FIRST_UTTERANCE,
        )
        # This production worker call persists the policy response with
        # record_model_decision before it submits the exact review batch.
        first_review = jobs.advance(scope, lease, model_decision=first_response)
        assert first_review.events
        recorded_job = repository.read_job(scope, job.job_id)
        assert recorded_job.payload["last_decision"]["source"] == "policy"
        assert recorded_job.payload["last_decision"]["request_id"] == str(first_request.request_id)
        after_review = repository.read_world(scope, world_id, branch_id)
        assert after_review.state.current_tick == 0
        first_plan = next(item for item in after_review.state.npc_plans if item.actor_id == speaker.id)
        assert first_plan.status == "ready"
        assert not _private_knowledge_for_claim(after_review.state, _FIRST_UTTERANCE)

        first_execution = jobs.advance(scope, lease)
        assert first_execution.outcome == "advanced"
        first_speech, = [
            event for event in first_execution.events
            if isinstance(event, KnowledgeAcquiredEvent)
            and _FIRST_UTTERANCE in event.record.claim
        ]
        assert first_speech.record.owner_id == speaker.id
        assert first_speech.record.holder_id == recipient.id
        assert first_speech.visibility == {
            "policy": "subject_ids",
            "subject_ids": sorted((str(speaker.id), str(recipient.id))),
        }
        assert not any(isinstance(event, TimeAdvancedEvent) for event in first_execution.events)
        after_first_speech = repository.read_world(scope, world_id, branch_id)
        first_completed_plan, = [
            item for item in after_first_speech.state.npc_plans
            if item.plan_id == first_plan.plan_id
        ]
        assert first_completed_plan.status == "completed"
        assert first_completed_plan.current_step == 1
        assert after_first_speech.state.current_tick == 0
        assert len(_private_knowledge_for_claim(after_first_speech.state, _FIRST_UTTERANCE)) == 1

        speaker_projection = project_player_state(
            after_first_speech.state.model_dump(mode="json"), str(speaker.id),
        )
        recipient_projection = project_player_state(
            after_first_speech.state.model_dump(mode="json"), str(recipient.id),
        )
        player_projection = project_player_state(
            after_first_speech.state.model_dump(mode="json"), str(player_id),
        )
        assert _FIRST_UTTERANCE in json.dumps(speaker_projection, ensure_ascii=False)
        assert _FIRST_UTTERANCE in json.dumps(recipient_projection, ensure_ascii=False)
        assert _FIRST_UTTERANCE not in json.dumps(player_projection, ensure_ascii=False)
        stored_speech = next(
            record for record in repository.list_events(scope, world_id, branch_id, limit=500).events
            if record.event_id == first_speech.event_id
        )
        assert project_event(stored_speech.model_dump(mode="json"), str(player_id)) is None

        # Genesis plus the real first speech commit must already replay cleanly.
        replay_before_takeover = repository.replay(
            scope, world_id, branch_id, from_genesis=True,
        )
        assert replay_before_takeover.state_hash == after_first_speech.state_hash

        second_pending = None
        for _ in range(8):
            current_job = repository.read_job(scope, job.job_id)
            assert current_job.status.value == "running"
            candidate = jobs.advance(scope, lease)
            if candidate.outcome == "decision_required":
                second_pending = candidate
                break
        assert second_pending is not None, "The finite second goal review should become due before tick 120."
        second_request, = second_pending.decision_requests
        assert second_request.kind == "npc.plan_review"
        assert second_request.actor_id == speaker.id
        before_takeover_review = repository.read_world(scope, world_id, branch_id)
        assert before_takeover_review.state.current_tick == _REVIEW_INTERVAL
        second_response = _test_plan(
            second_request, before_takeover_review.state, speaker.id, recipient.id,
            world.world.ruleset_version, _TAKEOVER_UTTERANCE,
        )
        second_review = jobs.advance(scope, lease, model_decision=second_response)
        assert second_review.events
        before_control = repository.read_world(scope, world_id, branch_id)
        second_plan, = [
            item for item in before_control.state.npc_plans
            if item.actor_id == speaker.id and item.status == "ready"
        ]
        assert second_plan.plan_id != first_plan.plan_id
        assert before_control.state.current_tick == _REVIEW_INTERVAL
        assert not _private_knowledge_for_claim(before_control.state, _TAKEOVER_UTTERANCE)

        repository.grant_actor_control(scope, world_id, takeover_user_id, speaker.id)
        takeover_execution = jobs.advance(scope, lease)
        assert takeover_execution.outcome == "advanced"
        assert not any(
            isinstance(event, KnowledgeAcquiredEvent)
            and _TAKEOVER_UTTERANCE in event.record.claim
            for event in takeover_execution.events
        )
        assert not any(isinstance(event, TimeAdvancedEvent) for event in takeover_execution.events)
        after_takeover = repository.read_world(scope, world_id, branch_id)
        abandoned_plan, = [
            item for item in after_takeover.state.npc_plans
            if item.plan_id == second_plan.plan_id
        ]
        assert abandoned_plan.status == "abandoned"
        assert abandoned_plan.last_failure == "The actor is under direct account control."
        assert after_takeover.state.current_tick == _REVIEW_INTERVAL
        assert not _private_knowledge_for_claim(after_takeover.state, _TAKEOVER_UTTERANCE)

        replay_after_takeover = repository.replay(
            scope, world_id, branch_id, from_genesis=True,
        )
        assert replay_after_takeover.state_hash == after_takeover.state_hash
    finally:
        database.dispose()
