from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import DepthProfile, RoleTask, SpeedProfile
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.npc_planning import NpcPlanningApplication
from world_engine.content import ContentCatalog
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.models import NpcPlanCreatedEvent, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.npc_plan_events import npc_plan_event_id
from world_engine.domain.npc_plans import NpcPlanStep
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules.contracts import RuleContext
from world_engine.simulation.contracts import SimulationContext
from world_engine.simulation.npc_capabilities import (
    build_default_npc_capability_registry,
    capability_id_for_action,
    preflight_npc_plan_step,
)
from world_engine.simulation.npc_plan_execution import build_npc_plan_execution_transition


@pytest.fixture(scope="module")
def catalog_world():
    draft = ContentCatalog().build_world("modern_city", "NPC private speech tests", "Test Player", seed=71)
    return draft, draft.state


def _context(actor_id: UUID, *, command_id: UUID | None = None) -> RuleContext:
    return RuleContext(
        world_id=UUID("10000000-0000-0000-0000-000000000001"),
        branch_id=UUID("20000000-0000-0000-0000-000000000002"),
        command_id=command_id or uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _active_people(state: WorldState) -> list[WorldEntity]:
    return [entity for entity in state.entities.values()
            if entity.kind in {"character", "person"} and entity.status == "active"]


def _scene_pair(state: WorldState) -> tuple[WorldEntity, WorldEntity]:
    people = _active_people(state)
    for actor in people:
        if actor.id == state.player_id or actor.location_id is None:
            continue
        target = next((item for item in people if item.id != actor.id and item.location_id == actor.location_id), None)
        if target is not None:
            return actor, target
    raise AssertionError("the catalog fixture must have two active people in one scene")


def _step(arguments: dict[str, object]) -> NpcPlanStep:
    return NpcPlanStep(capability_id=capability_id_for_action("speak"), arguments=arguments)


def _hidden_person_at(state: WorldState, actor: WorldEntity) -> tuple[WorldState, UUID]:
    hidden_id = uuid4()
    raw = state.model_dump(mode="json")
    raw["entities"][str(hidden_id)] = WorldEntity(
        id=hidden_id,
        kind="character",
        status="active",
        location_id=actor.location_id,
        name="Hidden scene participant",
        components={"hidden": True},
    ).model_dump(mode="json")
    return WorldState.model_validate(raw), hidden_id


def test_registry_registers_speak_as_a_strict_non_host_action(catalog_world):
    registry = build_default_npc_capability_registry()
    capability = registry.get("world.action.speak.v1")
    assert capability is not None
    assert capability.action_kind == "speak"
    assert capability.intent_model.__name__ == "SpeakIntent"
    assert all(not item.capability_id.startswith("world.action.request_") for item in registry)


@pytest.mark.parametrize(
    "arguments",
    [
        {"target_ids": [], "utterance": "hello"},
        {"target_ids": [str(uuid4())], "utterance": "x" * 1201},
        {"target_ids": [str(uuid4())], "utterance": "hello", "extra": "forbidden"},
    ],
)
def test_preflight_rejects_invalid_speech_schema_without_mutating_world(catalog_world, arguments):
    _draft, state = catalog_world
    actor, _target = _scene_pair(state)
    before = state_hash(state)

    result = preflight_npc_plan_step(state, actor.id, _step(arguments), _context(actor.id))

    assert result.status == "rejected"
    assert result.resolution.outcome.code == "invalid_npc_action_arguments"
    assert result.proposed_events == ()
    assert result.state_hash_before == result.state_hash_after == before


def test_preflight_only_accepts_known_same_scene_targets_and_keeps_player_controlled_target_as_recipient(catalog_world):
    _draft, state = catalog_world
    actor, _nearby = _scene_pair(state)
    player = state.entities[state.player_id]
    raw = state.model_dump(mode="json")
    player_raw = raw["entities"][str(player.id)]
    player_raw["location_id"] = str(actor.location_id)
    co_located = WorldState.model_validate(raw)
    before = state_hash(co_located)

    received = preflight_npc_plan_step(
        co_located,
        actor.id,
        _step({"target_ids": [str(player.id)], "utterance": "Please hear this privately."}),
        _context(actor.id),
        externally_controlled_actor_ids={player.id},
    )
    hidden_state, hidden_id = _hidden_person_at(co_located, co_located.entities[actor.id])
    hidden = preflight_npc_plan_step(
        hidden_state,
        actor.id,
        _step({"target_ids": [str(hidden_id)], "utterance": "No hidden targets."}),
        _context(actor.id),
    )
    far_target = next(
        item for item in _active_people(co_located)
        if item.id not in {actor.id, player.id} and item.location_id != actor.location_id
    )
    far = preflight_npc_plan_step(
        co_located,
        actor.id,
        _step({"target_ids": [str(far_target.id)], "utterance": "No distant targets."}),
        _context(actor.id),
    )

    assert received.status == "instant"
    assert received.resolution.elapsed_ticks == 0
    assert len(received.proposed_events) == 1
    assert received.proposed_events[0].record.holder_id == player.id
    assert received.state_hash_before == received.state_hash_after == before
    assert hidden.status == "rejected"
    assert hidden.resolution.outcome.code == "npc_reference_not_known"
    assert hidden.proposed_events == ()
    assert far.status == "rejected"
    assert far.resolution.outcome.code == "npc_reference_not_known"
    assert far.proposed_events == ()


class FakeModels:
    def __init__(self, output: object):
        self.output = output
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        return SimpleNamespace(normalized_output=self.output)


def _with_goal(state: WorldState, actor_id: UUID) -> tuple[WorldState, UUID]:
    goal_id = uuid4()
    raw = state.model_dump(mode="json")
    raw["goals"].append({
        "id": str(goal_id),
        "owner_id": str(actor_id),
        "title": "Practice honest conversation",
        "description": "Make room for a direct private exchange.",
        "status": "active",
        "priority": 90,
        "created_tick": state.current_tick,
        "updated_tick": state.current_tick,
    })
    return WorldState.model_validate(raw), goal_id


@pytest.mark.asyncio
async def test_planner_schema_and_real_execution_carry_private_speech_without_answering_target(catalog_world):
    draft, base_state = catalog_world
    actor, _nearby = _scene_pair(base_state)
    player = base_state.entities[base_state.player_id]
    raw = base_state.model_dump(mode="json")
    raw["entities"][str(player.id)]["location_id"] = str(actor.location_id)
    state, goal_id = _with_goal(WorldState.model_validate(raw), actor.id)
    utterance = "I remember that the rain arrived before the market closed."
    request = MessageRequest(
        request_id=uuid4(), world_id=draft.world_id, branch_id=draft.branch_id, session_id=uuid4(),
        actor_id=actor.id, expected_state_version="3", text="must not enter the decision prompt",
        profile=QualityProfile(speed="fast", depth="standard"),
    )
    fake = FakeModels({
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [{
            "capability_id": "world.action.speak.v1",
            "arguments": {"target_ids": [str(player.id)], "utterance": utterance},
        }],
        "reason": "Share a direct memory with the person present.",
    })
    before_hash, tick = state_hash(state), state.current_tick

    proposal = await NpcPlanningApplication(fake).propose(
        SimpleNamespace(), request, state, actor.id, state_version=3,
        externally_controlled_actor_ids={player.id}, ruleset_version=draft.ruleset_version,
    )

    assert proposal.status == "ready"
    assert proposal.plan is not None
    assert proposal.first_step_preflight is not None
    assert proposal.first_step_preflight.status == "instant"
    assert proposal.first_step_preflight.resolution.elapsed_ticks == 0
    assert len(fake.calls) == 1
    _, model_request = fake.calls[0]
    assert model_request.role_task is RoleTask.NPC_DECISION
    assert model_request.profile.speed is SpeedProfile.FAST
    assert model_request.profile.depth is DepthProfile.STANDARD
    assert "world.action.speak.v1" in str(model_request.output_schema)
    assert "world.action.request_" not in str(model_request.output_schema)
    assert "must not enter the decision prompt" not in model_request.messages[1].content
    assert proposal.state_hash == before_hash and proposal.current_tick == tick
    assert state_hash(state) == before_hash and state.current_tick == tick

    created = NpcPlanCreatedEvent(
        event_id=npc_plan_event_id(request.request_id, draft.ruleset_version, "created", proposal.plan.plan_id, 1),
        world_id=draft.world_id, branch_id=draft.branch_id, command_id=request.request_id,
        actor_id=actor.id, ruleset_version=draft.ruleset_version, plan=proposal.plan,
    )
    state_with_plan = apply_event(state, created)
    transition = build_npc_plan_execution_transition(
        state_with_plan,
        proposal.plan.plan_id,
        SimulationContext(
            world_id=draft.world_id,
            branch_id=draft.branch_id,
            command_id=uuid4(),
            command_actor_id=None,
            world_seed="npc-speech-test",
            state_version=4,
            ruleset_version=draft.ruleset_version,
            externally_controlled_actor_ids=frozenset({player.id}),
        ),
    )
    speech_events = [event for event in transition.events if isinstance(event, KnowledgeAcquiredEvent)]
    assert transition.outcome == "step_completed"
    assert len(speech_events) == 1
    assert speech_events[0].record.holder_id == player.id
    assert speech_events[0].record.owner_id == actor.id
    assert utterance in speech_events[0].record.claim
    assert not any(isinstance(event, TimeAdvancedEvent) for event in transition.events)
    assert transition.tick_before == transition.tick_after == tick
    assert state_hash(state) == before_hash


@pytest.mark.asyncio
async def test_planner_rejects_speech_to_an_unseen_person_before_first_step_preflight(catalog_world):
    draft, base_state = catalog_world
    actor, _target = _scene_pair(base_state)
    state, hidden_id = _hidden_person_at(base_state, actor)
    state, goal_id = _with_goal(state, actor.id)
    request = MessageRequest(
        request_id=uuid4(), world_id=draft.world_id, branch_id=draft.branch_id, session_id=uuid4(),
        actor_id=actor.id, expected_state_version="3", text="must not enter the decision prompt",
        profile=QualityProfile(speed="fast", depth="standard"),
    )
    fake = FakeModels({
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [{
            "capability_id": "world.action.speak.v1",
            "arguments": {"target_ids": [str(hidden_id)], "utterance": "A hidden audience is unknown."},
        }],
        "reason": "Try to speak to a person the actor cannot see.",
    })
    before_hash, tick = state_hash(state), state.current_tick

    proposal = await NpcPlanningApplication(fake).propose(
        SimpleNamespace(), request, state, actor.id, state_version=3,
        ruleset_version=draft.ruleset_version,
    )

    assert proposal.status == "invalid"
    assert proposal.error_code == "invalid_npc_plan_steps"
    assert proposal.plan is None
    assert proposal.first_step_preflight is None
    assert len(fake.calls) == 1
    assert state_hash(state) == before_hash and state.current_tick == tick
