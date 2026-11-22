from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Literal
from uuid import UUID, uuid4, uuid5

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from world_engine.adapters.models import RoleTask, SpeedProfile, DepthProfile
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.content import ContentCatalog
from world_engine.content.npc_autonomy_opening import (
    NPC_AUTONOMY_PROFILE_KEY,
    NPC_AUTONOMY_LIMITED_PROFILE,
    enable_diverse_npc_autonomy,
    enable_limited_npc_autonomy,
)
from world_engine.domain.components import (
    GoalCondition, GoalRecord, KnowledgeRecord, PlaceComponent, PlaceRoute,
    RelationObligation, RelationRecord, VisibilityScope, SkillsComponent,
)
from world_engine.domain.models import AccessScope, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.npc_plan_review import NPC_PLAN_REVIEW_HANDLER_ID
from world_engine.domain.npc_plans import NPC_SHARED_EXPLORATION_POLICY, NpcPlan, NpcPlanStep
from world_engine.domain.reducers import state_hash
import world_engine.application.npc_planning as npc_planning_module
from world_engine.application.npc_planning import (
    NpcPlanningApplication,
    _output_schema,
    _planning_market_view,
    _rewrite_refs,
    _validate_schema_refs,
)
from world_engine.rules.contracts import IntentModel
from world_engine.simulation.npc_capabilities import capability_id_for_action
from world_engine.simulation.npc_capabilities import NpcCapabilityRegistry, build_default_npc_capability_registry


@pytest.fixture(scope="module")
def full_state() -> WorldState:
    return ContentCatalog().build_world("modern_city", "NPC planning tests", "Test Player").state


@pytest.fixture(scope="module")
def feudal_autonomy_states():
    draft = ContentCatalog().build_world(
        "low_magic_feudal",
        "NPC planning autonomy tests",
        "Test Player",
        seed=20261121,
        population_mode="sustained",
        pack_version="2.9.0",
    )
    return (
        draft,
        enable_diverse_npc_autonomy(draft.state, world_id=draft.world_id),
        enable_limited_npc_autonomy(draft.state, world_id=draft.world_id),
    )


def _scheduled_reserve_actor(state: WorldState) -> tuple[WorldEntity, GoalRecord]:
    schedules = sorted(
        (
            schedule for schedule in state.scheduled_events
            if schedule.handler_id == NPC_PLAN_REVIEW_HANDLER_ID
        ),
        key=lambda schedule: (schedule.due_tick, str(schedule.id)),
    )
    for schedule in schedules:
        actor_id = UUID(schedule.payload["actor_id"])
        actor = state.entities[actor_id]
        goal = next((
            goal for goal in state.goals
            if goal.owner_id == actor_id
            and goal.status == "active"
            and len(goal.conditions) == 1
            and goal.conditions[0].kind == "resource_balance_minimum"
        ), None)
        if goal is not None:
            return actor, goal
    raise AssertionError("the opt-in feudal seed must schedule a reserve-goal resident")


def _world_request(draft, actor: WorldEntity) -> MessageRequest:
    return _request(actor.id, expected_state_version="0").model_copy(update={
        "world_id": draft.world_id,
        "branch_id": draft.branch_id,
    })


def _active_actor_with_skill(state: WorldState) -> tuple[WorldEntity, str]:
    for entity in state.entities.values():
        if entity.kind not in {"character", "person"} or entity.status != "active" or entity.id == state.player_id:
            continue
        skills = SkillsComponent.model_validate(entity.components.get("skills", {}))
        if skills.skills:
            return entity, next(iter(sorted(skills.skills)))
    raise AssertionError("full catalog must include an active non-player person with a registered skill")


def _request(actor_id: UUID, *, expected_state_version: str = "7") -> MessageRequest:
    return MessageRequest(
        request_id=uuid4(),
        world_id=UUID("10000000-0000-0000-0000-000000000001"),
        branch_id=UUID("20000000-0000-0000-0000-000000000002"),
        session_id=uuid4(),
        actor_id=actor_id,
        expected_state_version=expected_state_version,
        text="GM_QUERY_MUST_NOT_ENTER_NPC_PROMPT",
        profile=QualityProfile(speed="fast", depth="standard"),
    )


def _scope() -> AccessScope:
    return AccessScope(tenant_id=uuid4(), user_id=uuid4())


def _with_goal(state: WorldState, actor_id: UUID, *, title: str = "OWN_ACTIVE_GOAL") -> tuple[WorldState, UUID]:
    goal_id = uuid4()
    raw = state.model_dump(mode="json")
    raw["goals"].append({
        "id": str(goal_id),
        "owner_id": str(actor_id),
        "title": title,
        "description": "A current personal objective.",
        "status": "active",
        "priority": 90,
        "created_tick": state.current_tick,
        "updated_tick": state.current_tick,
    })
    return WorldState.model_validate(raw), goal_id


def _profile_candidate_state(state: WorldState, actor_id: UUID):
    other_id = next(
        entity.id for entity in state.entities.values()
        if entity.kind in {"character", "person"} and entity.id not in {actor_id, state.player_id}
    )
    current = state.current_tick
    eligible = [
        GoalRecord(
            id=UUID(int=1000 + index), owner_id=actor_id,
            title=f"PROFILE_ELIGIBLE_GOAL_{index}", description="Independent synthetic objective.",
            status="active", priority=priority, created_tick=current, updated_tick=current,
        )
        for index, priority in enumerate((100, 90, 90, 80, 70, 60, 50, 40), start=1)
    ]
    hidden_place_id = UUID(int=9000)
    hidden_place = WorldEntity(
        id=hidden_place_id, kind="place", name="PROFILE_HIDDEN_PLACE_SENTINEL",
        components={"place": PlaceComponent(place_type="site").model_dump(mode="json"), "hidden": True},
    )
    excluded = [
        GoalRecord(
            id=UUID(int=1), owner_id=actor_id, title="PROFILE_HIDDEN_GOAL_SENTINEL",
            status="active", priority=100, created_tick=current, updated_tick=current,
            conditions=[GoalCondition(kind="reach_location", subject_id=actor_id, target_id=hidden_place_id)],
        ),
        GoalRecord(
            id=UUID(int=2), owner_id=actor_id, title="PROFILE_EXPIRED_GOAL_SENTINEL",
            status="active", priority=100, created_tick=current, updated_tick=current,
            deadline=current - 1,
        ),
        GoalRecord(
            id=UUID(int=3), owner_id=actor_id, title="PROFILE_PAUSED_GOAL_SENTINEL",
            status="paused", priority=100, created_tick=current, updated_tick=current,
        ),
        GoalRecord(
            id=UUID(int=4), owner_id=other_id, title="PROFILE_FOREIGN_GOAL_SENTINEL",
            status="active", priority=100, created_tick=current, updated_tick=current,
        ),
    ]
    obligation_text = "PROFILE_ACTIVE_OBLIGATION_MUST_REMAIN_VISIBLE"
    relation = RelationRecord(
        id=UUID(int=9100), from_id=actor_id, to_id=other_id, updated_tick=current,
        visibility=VisibilityScope(policy="public"),
        obligations=[RelationObligation(
            obligation_id=UUID(int=9101), kind="visit", description=obligation_text,
            beneficiary_id=other_id, due_tick=current + 10, status="active",
        )],
    )
    raw = state.model_dump(mode="json")
    raw["goals"] = [goal.model_dump(mode="json") for goal in [*eligible, *excluded]]
    raw["entities"][str(hidden_place_id)] = hidden_place.model_dump(mode="json")
    raw["relations"] = [*raw.get("relations", []), relation.model_dump(mode="json")]
    return WorldState.model_validate(raw), [goal.id for goal in eligible], [goal.id for goal in excluded], obligation_text


def _goal_prerequisite_graph(state: WorldState, actor_id: UUID):
    other_id = next(
        entity.id for entity in state.entities.values()
        if entity.kind in {"character", "person"} and entity.id not in {actor_id, state.player_id}
    )
    tick = state.current_tick
    root_a, root_b, shared, middle, future, foreign, missing = (uuid4() for _ in range(7))
    goals = [
        GoalRecord(
            id=root_a, owner_id=actor_id, title="ROOT_A", status="active", priority=100,
            created_tick=tick, updated_tick=tick,
            conditions=[GoalCondition(kind="goal_completed", target_id=shared)],
        ),
        GoalRecord(
            id=root_b, owner_id=actor_id, title="ROOT_B", status="active", priority=90,
            created_tick=tick, updated_tick=tick,
            conditions=[GoalCondition(kind="goal_completed", target_id=shared)],
        ),
        GoalRecord(
            id=shared, owner_id=actor_id, title="SHARED_PAUSED", status="paused",
            created_tick=tick, updated_tick=tick,
            conditions=[
                GoalCondition(kind="goal_completed", target_id=middle),
                GoalCondition(kind="goal_completed", target_id=future),
                GoalCondition(kind="goal_completed", target_id=foreign),
                GoalCondition(kind="goal_completed", target_id=missing),
                GoalCondition(kind="entity_status", subject_id=actor_id, status="SECRET_CONDITION_SENTINEL"),
            ],
        ),
        GoalRecord(
            id=middle, owner_id=actor_id, title="MIDDLE_PAUSED", status="paused",
            created_tick=tick, updated_tick=tick,
            conditions=[GoalCondition(kind="goal_completed", target_id=shared)],
        ),
        GoalRecord(
            id=future, owner_id=actor_id, title="FUTURE_GOAL_SENTINEL", status="active",
            created_tick=tick + 1, updated_tick=tick + 1,
        ),
        GoalRecord(
            id=foreign, owner_id=other_id, title="FOREIGN_GOAL_SENTINEL", status="paused",
            created_tick=tick, updated_tick=tick,
        ),
    ]
    # Deliberately retain one dangling prerequisite to exercise fail-closed
    # handling of a malformed/unknown edge that strict WorldState validation
    # would normally reject.
    return state.model_copy(update={"goals": goals}), (root_a, root_b, shared, middle, future, foreign, missing)


def _goal_prerequisite_fanout_state(state: WorldState, actor_id: UUID):
    tick = state.current_tick
    middle_ids = [uuid4() for _ in range(8)]
    leaf_ids = [[uuid4() for _ in range(8)] for _ in middle_ids]
    root = GoalRecord(
        id=uuid4(), owner_id=actor_id, title="FANOUT_ROOT", status="active", priority=100,
        created_tick=tick, updated_tick=tick,
        conditions=[GoalCondition(kind="goal_completed", target_id=goal_id) for goal_id in middle_ids],
    )
    middle = [
        GoalRecord(
            id=goal_id, owner_id=actor_id, title=f"FANOUT_MIDDLE_{index}", status="paused",
            created_tick=tick, updated_tick=tick,
            conditions=[GoalCondition(kind="goal_completed", target_id=target_id) for target_id in leaf_ids[index]],
        )
        for index, goal_id in enumerate(middle_ids)
    ]
    leaves = [
        GoalRecord(
            id=goal_id, owner_id=actor_id, title=f"FANOUT_LEAF_{index}", status="paused",
            created_tick=tick, updated_tick=tick,
        )
        for index, goals in enumerate(leaf_ids)
        for goal_id in goals
    ]
    raw = state.model_dump(mode="json")
    raw["goals"] = [goal.model_dump(mode="json") for goal in [root, *middle, *leaves]]
    return WorldState.model_validate(raw), set(middle_ids), {goal_id for row in leaf_ids for goal_id in row}


def _with_sentinel_knowledge(state: WorldState, actor_id: UUID, other_id: UUID) -> WorldState:
    raw = state.model_dump(mode="json")
    raw["knowledge"].extend([
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, claim="OWN_KNOWN_FACT_SENTINEL", received_tick=state.current_tick,
            visibility=VisibilityScope(policy="private"), kind="memory",
        ).model_dump(mode="json"),
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, claim="FUTURE_FACT_SENTINEL", received_tick=state.current_tick,
            valid_from_tick=state.current_tick + 1,
            visibility=VisibilityScope(policy="private"), kind="memory",
        ).model_dump(mode="json"),
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, claim="AUTHOR_ONLY_FACT_SENTINEL", received_tick=state.current_tick,
            visibility=VisibilityScope(policy="author_only"), kind="memory",
        ).model_dump(mode="json"),
        KnowledgeRecord(
            id=uuid4(), owner_id=other_id, claim="OTHER_PERSON_PRIVATE_FACT_SENTINEL", received_tick=state.current_tick,
            visibility=VisibilityScope(policy="private"), kind="memory",
        ).model_dump(mode="json"),
    ])
    return WorldState.model_validate(raw)


def _with_hidden_routed_place(state: WorldState, actor_id: UUID) -> tuple[WorldState, UUID]:
    actor = state.entities[actor_id]
    origin = state.entities[actor.location_id]
    place = PlaceComponent.model_validate(origin.components["place"])
    hidden_id = uuid4()
    hidden_place = WorldEntity(
        id=hidden_id,
        kind="place",
        name="UNOBSERVED_AUTHOR_ONLY_PLACE_SENTINEL",
        components={"place": PlaceComponent(place_type="site").model_dump(mode="json"), "hidden": True},
    )
    route = PlaceRoute(
        route_id=uuid4(),
        from_place_id=origin.id,
        to_place_id=hidden_id,
        travel_ticks=600,
    )
    raw = state.model_dump(mode="json")
    raw["entities"][str(origin.id)] = origin.model_copy(update={
        "components": {
            **origin.components,
            "place": place.model_copy(update={"routes": [*place.routes, route]}).model_dump(mode="json"),
        },
    }).model_dump(mode="json")
    raw["entities"][str(hidden_id)] = hidden_place.model_dump(mode="json")
    return WorldState.model_validate(raw), hidden_id


def _plan(goal_id: UUID, skill_id: str) -> dict[str, object]:
    return {
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [{
            "capability_id": capability_id_for_action("train"),
            "arguments": {"skill_id": skill_id, "duration_ticks": 600},
        }],
        "reason": "Practice a skill relevant to the selected goal.",
    }


class FakeModels:
    def __init__(self, output: object = None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls: list[tuple[AccessScope, object]] = []

    async def generate(self, scope: AccessScope, request):
        self.calls.append((scope, request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(normalized_output=self.output)


class ContextualFakeModels(FakeModels):
    def __init__(self, output_factory):
        super().__init__()
        self.output_factory = output_factory

    async def generate(self, scope: AccessScope, request):
        self.calls.append((scope, request))
        self.output = self.output_factory(request)
        return SimpleNamespace(normalized_output=self.output)


@pytest.mark.asyncio
async def test_ready_plan_uses_only_the_npc_knowledge_and_returns_snapshot_bound_preflight(full_state: WorldState):
    actor, skill_id = _active_actor_with_skill(full_state)
    state, goal_id = _with_goal(full_state, actor.id)
    other = next(entity for entity in state.entities.values() if entity.kind in {"character", "person"} and entity.id not in {actor.id, state.player_id})
    state, other_goal_id = _with_goal(state, other.id, title="OTHER_NPC_GOAL_SENTINEL")
    state = _with_sentinel_knowledge(state, actor.id, other.id)
    state = state.model_copy(update={"metadata": state.metadata | {NPC_SHARED_EXPLORATION_POLICY: True}})
    request = _request(actor.id)
    models = FakeModels(_plan(goal_id, skill_id))
    original_hash = state_hash(state)
    original_tick = state.current_tick

    proposal = await NpcPlanningApplication(models).propose(
        _scope(), request, state, actor.id,
        state_version=7,
        ruleset_version="2.6.0",
    )

    assert proposal.status == "ready"
    assert proposal.plan is not None
    assert proposal.plan.status == "ready"
    assert proposal.plan.current_step == 0
    assert proposal.plan.next_review_tick == state.current_tick
    assert proposal.plan.created_tick == proposal.plan.updated_tick == state.current_tick
    assert proposal.plan.goal_ids == [goal_id]
    assert proposal.plan.plan_id == uuid5(request.request_id, f"npc.plan.v1:{actor.id}:7")
    assert proposal.state_version == 7
    assert proposal.ruleset_version == "2.6.0"
    assert proposal.current_tick == original_tick
    assert proposal.state_hash == original_hash
    assert proposal.first_step_preflight is not None
    assert proposal.first_step_preflight.status == "interval_required"
    assert proposal.first_step_preflight.required_ticks == 600
    assert len(models.calls) == 1
    _, model_request = models.calls[0]
    assert model_request.role_task is RoleTask.NPC_DECISION
    assert model_request.profile.speed is SpeedProfile.FAST
    assert model_request.profile.depth is DepthProfile.STANDARD
    user_context = model_request.messages[1].content
    context = json.loads(user_context)
    assert context["shared_exploration_available"] is True
    assert context["shared_trade_available"] is False
    assert "OWN_ACTIVE_GOAL" in user_context
    assert "OWN_KNOWN_FACT_SENTINEL" in user_context
    assert "OTHER_NPC_GOAL_SENTINEL" not in user_context
    assert "OTHER_PERSON_PRIVATE_FACT_SENTINEL" not in user_context
    assert "FUTURE_FACT_SENTINEL" not in user_context
    assert "AUTHOR_ONLY_FACT_SENTINEL" not in user_context
    assert "GM_QUERY_MUST_NOT_ENTER_NPC_PROMPT" not in user_context
    allowed_goal_ids = model_request.output_schema["oneOf"][0]["properties"]["goal_ids"]["items"]["enum"]
    assert str(goal_id) in allowed_goal_ids
    assert str(other_goal_id) not in allowed_goal_ids
    own_active_ids = {
        str(goal.id) for goal in state.goals
        if goal.owner_id == actor.id and goal.status == "active" and goal.created_tick <= state.current_tick
    }
    assert set(allowed_goal_ids).issubset(own_active_ids)
    assert len(allowed_goal_ids) <= 8
    assert state.current_tick == original_tick
    assert state_hash(state) == original_hash


@pytest.mark.asyncio
async def test_profile_candidate_windows_follow_depth_after_eligibility_and_stable_priority(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, eligible_ids, excluded_ids, obligation_text = _profile_candidate_state(full_state, actor.id)
    priorities = dict(zip(eligible_ids, (100, 90, 90, 80, 70, 60, 50, 40), strict=True))
    ordered_eligible_ids = sorted(
        eligible_ids,
        key=lambda goal_id: (-priorities[goal_id], True, 0, state.current_tick, str(goal_id)),
    )
    windows = {"standard": 2, "deep": 4, "exhaustive": 6}
    observed_orders = {depth: [] for depth in windows}

    for speed in ("fast", "balanced", "patient"):
        for depth, expected_count in windows.items():
            models = FakeModels({"kind": "defer", "reason": "Wait before choosing a goal."})
            request = _request(actor.id).model_copy(update={
                "profile": QualityProfile(speed=speed, depth=depth),
            })
            proposal = await NpcPlanningApplication(models).propose(
                _scope(), request, state, actor.id, state_version=7,
            )
            assert proposal.status == "defer"
            model_request = models.calls[0][1]
            context = json.loads(model_request.messages[1].content)
            active_goal_ids = [UUID(row["id"]) for row in context["active_goals"]]
            expected_ids = ordered_eligible_ids[:expected_count]

            assert active_goal_ids == expected_ids
            observed_orders[depth].append(active_goal_ids)
            assert not set(active_goal_ids).intersection(excluded_ids)
            assert str(excluded_ids[0]) not in json.dumps(context)
            assert str(excluded_ids[1]) not in json.dumps(context)
            assert str(excluded_ids[2]) not in json.dumps(context)
            assert str(excluded_ids[3]) not in json.dumps(context)
            goal_schema = model_request.output_schema["oneOf"][0]["properties"]["goal_ids"]
            assert set(goal_schema["items"]["enum"]) == {str(goal_id) for goal_id in expected_ids}
            assert goal_schema["maxItems"] == expected_count
            assert f"一至{expected_count}个" in model_request.messages[0].content
            obligations = context["own_commitments"]["known_relation_obligations"]
            assert any(row["description"] == obligation_text and row["status"] == "active" for row in obligations)

    for orders in observed_orders.values():
        assert orders[0] == orders[1] == orders[2]


@pytest.mark.asyncio
async def test_profile_causal_expansion_adds_only_bounded_actor_owned_prerequisite_edges(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, ids = _goal_prerequisite_graph(full_state, actor.id)
    root_a, root_b, shared, middle, future, foreign, missing = ids
    scope = _scope()
    observed: dict[str, tuple[dict[str, object], str, set[str]]] = {}

    for depth in ("standard", "deep", "exhaustive"):
        models = FakeModels({"kind": "defer", "reason": "Wait before choosing a goal."})
        request = _request(actor.id).model_copy(update={
            "profile": QualityProfile(speed="balanced", depth=depth),
        })
        proposal = await NpcPlanningApplication(models).propose(
            scope, request, state, actor.id, state_version=7,
        )
        assert proposal.status == "defer"
        model_request = models.calls[0][1]
        context = json.loads(model_request.messages[1].content)
        allowed = set(model_request.output_schema["oneOf"][0]["properties"]["goal_ids"]["items"]["enum"])
        observed[depth] = (context, model_request.messages[0].content, allowed)

    standard, standard_system, standard_allowed = observed["standard"]
    deep, deep_system, deep_allowed = observed["deep"]
    exhaustive, exhaustive_system, exhaustive_allowed = observed["exhaustive"]
    expected_roots = [str(root_a), str(root_b)]
    assert [row["id"] for row in standard["active_goals"]] == expected_roots
    assert standard["active_goals"] == deep["active_goals"] == exhaustive["active_goals"]
    assert standard_allowed == deep_allowed == exhaustive_allowed == set(expected_roots)
    assert "goal_prerequisite_edges" not in standard
    assert "goal_prerequisite_edges" not in standard_system

    expected_shared_edge = {
        "layer": 2,
        "dependent_goal_id": str(shared),
        "prerequisite_goal_id": str(middle),
        "prerequisite_title": "MIDDLE_PAUSED",
        "prerequisite_status": "paused",
        "prerequisite_updated_tick": state.current_tick,
    }
    assert deep["goal_prerequisite_edges"] == [expected_shared_edge]
    assert expected_shared_edge in exhaustive["goal_prerequisite_edges"]
    assert exhaustive["goal_prerequisite_edges"] == [
        expected_shared_edge,
        {
            "layer": 3,
            "dependent_goal_id": str(middle),
            "prerequisite_goal_id": str(shared),
            "prerequisite_title": "SHARED_PAUSED",
            "prerequisite_status": "paused",
            "prerequisite_updated_tick": state.current_tick,
        },
    ]
    for system in (deep_system, exhaustive_system):
        assert "goal_prerequisite_edges" in system
        assert "未列出的边不能证明不存在其他前置条件" in system
        assert "不会授权选择更多 goal_id" in system

    # Direct conditions on eligible roots remain the only candidates and are
    # byte-for-byte identical at every depth. No other condition on a deeper
    # prerequisite is copied into the explanatory edge list.
    direct_condition = standard["active_goals"][0]["conditions"][0]
    assert direct_condition["target_id"] == str(shared)
    assert direct_condition["prerequisite_status"] == "paused"
    serialized = json.dumps(exhaustive, ensure_ascii=False)
    for excluded_id in (future, foreign, missing):
        assert str(excluded_id) not in serialized
    assert "FUTURE_GOAL_SENTINEL" not in serialized
    assert "FOREIGN_GOAL_SENTINEL" not in serialized
    assert "SECRET_CONDITION_SENTINEL" not in serialized
    assert all(
        row["dependent_goal_id"] not in {str(root_a), str(root_b)}
        for row in exhaustive["goal_prerequisite_edges"]
    )


@pytest.mark.asyncio
async def test_profile_causal_expansion_stops_at_global_edge_cap_and_is_deterministic(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, middle_ids, leaf_ids = _goal_prerequisite_fanout_state(full_state, actor.id)
    profile = QualityProfile(speed="patient", depth="exhaustive")
    scope = _scope()
    contexts = []

    for _ in range(2):
        models = FakeModels({"kind": "defer", "reason": "Wait before choosing a goal."})
        request = _request(actor.id).model_copy(update={"profile": profile})
        proposal = await NpcPlanningApplication(models).propose(
            scope, request, state, actor.id, state_version=7,
        )
        assert proposal.status == "defer"
        contexts.append(json.loads(models.calls[0][1].messages[1].content))

    edges = contexts[0]["goal_prerequisite_edges"]
    assert len(edges) == 24
    assert edges == contexts[1]["goal_prerequisite_edges"]
    assert len({(row["dependent_goal_id"], row["prerequisite_goal_id"]) for row in edges}) == 24
    assert {UUID(row["dependent_goal_id"]) for row in edges}.issubset(middle_ids)
    assert {UUID(row["prerequisite_goal_id"]) for row in edges}.issubset(leaf_ids)
    assert all(row["layer"] == 2 for row in edges)


@pytest.mark.asyncio
async def test_absent_or_incomplete_profile_keeps_legacy_goal_context(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, ids = _goal_prerequisite_graph(full_state, actor.id)
    template = _request(actor.id)
    scope = _scope()

    for profile in (None, SimpleNamespace(speed="balanced")):
        request_fields = {
            "request_id": template.request_id,
            "world_id": template.world_id,
            "branch_id": template.branch_id,
            "actor_id": actor.id,
            "expected_state_version": template.expected_state_version,
        }
        if profile is not None:
            request_fields["profile"] = profile
        request = SimpleNamespace(**request_fields)
        models = FakeModels({"kind": "defer", "reason": "Wait before choosing a goal."})
        proposal = await NpcPlanningApplication(models).propose(
            scope, request, state, actor.id, state_version=7,
        )
        assert proposal.status == "defer"
        model_request = models.calls[0][1]
        context = json.loads(model_request.messages[1].content)
        assert "goal_prerequisite_edges" not in context
        assert {row["id"] for row in context["active_goals"]} == {str(ids[0]), str(ids[1])}


@pytest.mark.asyncio
async def test_profiled_planner_rejects_model_goal_excluded_from_candidate_window(full_state: WorldState):
    actor, skill_id = _active_actor_with_skill(full_state)
    state, eligible_ids, _, _ = _profile_candidate_state(full_state, actor.id)
    excluded_from_standard_window = eligible_ids[2]
    models = FakeModels(_plan(excluded_from_standard_window, skill_id))
    request = _request(actor.id).model_copy(update={
        "profile": QualityProfile(speed="fast", depth="standard"),
    })

    proposal = await NpcPlanningApplication(models).propose(
        _scope(), request, state, actor.id, state_version=7,
    )

    assert proposal.status == "invalid"
    assert proposal.error_code == "npc_plan_goal_not_available"
    model_request = models.calls[0][1]
    context = json.loads(model_request.messages[1].content)
    assert str(excluded_from_standard_window) not in {row["id"] for row in context["active_goals"]}
    assert str(excluded_from_standard_window) not in model_request.output_schema["oneOf"][0]["properties"]["goal_ids"]["items"]["enum"]


@pytest.mark.asyncio
async def test_legacy_npc_planner_request_without_profile_keeps_eight_goal_window(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, eligible_ids, excluded_ids, obligation_text = _profile_candidate_state(full_state, actor.id)
    models = FakeModels({"kind": "defer", "reason": "Wait before choosing a goal."})
    template = _request(actor.id)
    legacy_request = SimpleNamespace(
        request_id=template.request_id,
        world_id=template.world_id,
        branch_id=template.branch_id,
        actor_id=actor.id,
        expected_state_version="7",
    )

    proposal = await NpcPlanningApplication(models).propose(
        _scope(), legacy_request, state, actor.id, state_version=7,
    )

    assert proposal.status == "defer"
    model_request = models.calls[0][1]
    context = json.loads(model_request.messages[1].content)
    active_goal_ids = {UUID(row["id"]) for row in context["active_goals"]}
    goal_schema = model_request.output_schema["oneOf"][0]["properties"]["goal_ids"]
    assert active_goal_ids == set(eligible_ids)
    assert not active_goal_ids.intersection(excluded_ids)
    assert goal_schema["maxItems"] == 8
    assert set(goal_schema["items"]["enum"]) == {str(goal_id) for goal_id in eligible_ids}
    assert model_request.profile.speed is SpeedProfile.BALANCED
    assert model_request.profile.depth is DepthProfile.DEEP
    assert any(
        row["description"] == obligation_text and row["status"] == "active"
        for row in context["own_commitments"]["known_relation_obligations"]
    )


@pytest.mark.asyncio
async def test_replanning_prompt_contains_only_own_safe_recent_failures(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    other = next(entity for entity in full_state.entities.values()
                 if entity.kind == "character" and entity.id not in {actor.id, full_state.player_id})
    state, goal_id = _with_goal(full_state, actor.id)
    state, other_goal = _with_goal(state, other.id)
    now = state.current_tick

    def blocked(owner, goal, message, tick=now):
        return NpcPlan(
            plan_id=uuid4(), actor_id=owner, goal_ids=[goal], revision=2, status="blocked",
            created_tick=tick, updated_tick=tick, current_step=0, next_review_tick=tick + 120,
            steps=[NpcPlanStep(capability_id=capability_id_for_action("trade"),
                               arguments={"listing_id": str(uuid4()), "quantity": "1", "raw": "PRIVATE_STEP_SENTINEL"})],
            attempts=1, last_failure=message, reason="AUTHOR_PLAN_SENTINEL",
        )

    own = blocked(actor.id, goal_id, "npc_trade_insufficient_stock: PRIVATE_RULE_DETAIL")
    state = state.model_copy(update={"npc_plans": sorted([
        own,
        blocked(other.id, other_goal, "npc_trade_market_quote_changed"),
        blocked(actor.id, goal_id, "npc_trade_seller_unavailable", now - 8 * state.calendar.day_seconds),
    ], key=lambda plan: str(plan.plan_id))})
    models = FakeModels({"kind": "defer", "reason": "Wait for a delivery."})
    proposal = await NpcPlanningApplication(models).propose(_scope(), _request(actor.id), state, actor.id, state_version=7)
    assert proposal.status == "defer"
    prompt = models.calls[0][1].messages[1].content
    assert json.loads(prompt)["own_recent_blocked_steps"] == [{
        "goal_ids": [str(goal_id)], "capability_id": "world.action.trade.v1",
        "at_tick": now, "attempts": 1, "outcome": "成交前存货已不足。",
    }]
    for private in ("PRIVATE_STEP_SENTINEL", "PRIVATE_RULE_DETAIL", "AUTHOR_PLAN_SENTINEL", str(own.plan_id)):
        assert private not in prompt


@pytest.mark.asyncio
async def test_controlled_or_player_actor_is_rejected_without_model_call(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, _ = _with_goal(full_state, actor.id)
    models = FakeModels({"kind": "defer", "reason": "later"})
    app = NpcPlanningApplication(models)

    controlled = await app.propose(
        _scope(), _request(actor.id), state, actor.id,
        state_version=7, externally_controlled_actor_ids={actor.id},
    )
    assert controlled.status == "invalid"
    assert controlled.error_code == "npc_actor_externally_controlled"
    assert not models.calls

    if state.player_id is not None:
        player_request = _request(state.player_id)
        player = await app.propose(_scope(), player_request, state, state.player_id, state_version=7)
        assert player.status == "invalid"
        assert player.error_code == "npc_actor_is_player"
        assert not models.calls


@pytest.mark.asyncio
async def test_invalid_model_output_unknown_goal_capability_reference_and_extra_fields_fail_closed(full_state: WorldState):
    actor, skill_id = _active_actor_with_skill(full_state)
    state, goal_id = _with_goal(full_state, actor.id)
    request = _request(actor.id)
    invalid_outputs = [
        {**_plan(goal_id, skill_id), "surprise": True},
        {**_plan(uuid4(), skill_id)},
        {
            "kind": "plan",
            "goal_ids": [str(goal_id)],
            "steps": [{"capability_id": "world.action.hack.v1", "arguments": {}}],
            "reason": "Try an unknown action.",
        },
        {
            "kind": "plan",
            "goal_ids": [str(goal_id)],
            "steps": [{
                "capability_id": capability_id_for_action("move"),
                "arguments": {"destination_id": str(uuid4())},
            }],
            "reason": "Try an unknown target.",
        },
    ]
    for output in invalid_outputs:
        models = FakeModels(output)
        before = state_hash(state)
        proposal = await NpcPlanningApplication(models).propose(_scope(), request, state, actor.id, state_version=7)
        assert proposal.status == "invalid"
        assert proposal.plan is None
        assert proposal.error_code in {"invalid_npc_plan_output", "npc_plan_goal_not_available", "invalid_npc_plan_steps"}
        assert len(models.calls) == 1
        assert state_hash(state) == before


@pytest.mark.asyncio
async def test_rule_rejected_first_step_is_an_invalid_proposal(full_state: WorldState):
    actor, destination_id = next(
        (entity, route.to_place_id)
        for entity in full_state.entities.values()
        if entity.kind in {"character", "person"}
        and entity.status == "active"
        and entity.id != full_state.player_id
        and entity.location_id is not None
        for place in [PlaceComponent.model_validate(full_state.entities[entity.location_id].components.get("place"))]
        for route in place.routes
    )
    goal_state, goal_id = _with_goal(full_state, actor.id)
    origin = goal_state.entities[actor.location_id]
    place = PlaceComponent.model_validate(origin.components["place"])
    routes = [
        route.model_copy(update={"is_open": False}) if route.to_place_id == destination_id else route
        for route in place.routes
    ]
    raw = goal_state.model_dump(mode="json")
    raw["entities"][str(origin.id)] = origin.model_copy(update={
        "components": {
            **origin.components,
            "place": place.model_copy(update={"routes": routes}).model_dump(mode="json"),
        },
    }).model_dump(mode="json")
    state = WorldState.model_validate(raw)
    models = FakeModels({
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [{"capability_id": capability_id_for_action("move"), "arguments": {"destination_id": str(destination_id)}}],
        "reason": "Try to travel.",
    })
    before, tick = state_hash(state), state.current_tick

    proposal = await NpcPlanningApplication(models).propose(_scope(), _request(actor.id), state, actor.id, state_version=7)

    assert proposal.status == "invalid"
    assert proposal.error_code == "npc_plan_first_step_unavailable"
    assert len(models.calls) == 1
    route_view = json.loads(models.calls[0][1].messages[1].content)["known_routes"]
    matching = [row for row in route_view if row["destination_id"] == str(destination_id)]
    assert matching and all(not row["access_allowed_now"] for row in matching)
    assert proposal.state_hash == before
    assert state.current_tick == tick
    assert state_hash(state) == before


def test_npc_market_budget_uses_only_the_actors_cash_and_visible_listings():
    actor, other = uuid4(), uuid4()
    currency = "core.coin"
    view = {
        "market_offers": {"currency_resource_id": currency, "offers": [{
            "listing_id": str(uuid4()), "resource_id": "core.ore",
            "unit_price_minor": "6", "minimum_quantity": "1.5",
        }]},
    }
    state = WorldState(
        entities={person: WorldEntity(id=person, kind="person", name="Resident") for person in (actor, other)},
        resources={
            uuid4(): ResourceAccount(resource_id=currency, balance="8", owner_id=actor),
            uuid4(): ResourceAccount(resource_id=currency, balance="100", owner_id=other),
        },
    )
    actor_market = _planning_market_view(state, actor, view)
    other_market = _planning_market_view(state, other, view)
    assert actor_market["own_currency_balance"] == "8"
    assert not actor_market["offers"][0]["minimum_within_budget"]
    assert other_market["offers"][0]["minimum_within_budget"]
    assert actor_market["offers"] == [view["market_offers"]["offers"][0] | {"minimum_within_budget": False}]
    assert "minimum_within_budget" not in view["market_offers"]["offers"][0]


@pytest.mark.asyncio
async def test_diverse_profile_offers_current_preflighted_reserve_purchase_and_can_defer(feudal_autonomy_states):
    draft, diverse_state, _ = feudal_autonomy_states
    actor, goal = _scheduled_reserve_actor(diverse_state)
    models = FakeModels({"kind": "defer", "reason": "I will wait."})
    before = state_hash(diverse_state)

    proposal = await NpcPlanningApplication(models).propose(
        _scope(),
        _world_request(draft, actor),
        diverse_state,
        actor.id,
        state_version=0,
        ruleset_version=draft.ruleset_version,
    )

    assert proposal.status == "defer"
    assert len(models.calls) == 1
    context = json.loads(models.calls[0][1].messages[1].content)
    known_view = context["known_view"]
    choices = known_view["preflighted_reserve_purchase_choices"]
    assert 1 <= len(choices) <= 4
    choice = next(row for row in choices if row["goal_id"] == str(goal.id))
    assert choice["capability_id"] == capability_id_for_action("trade")
    assert set(choice) == {"goal_id", "capability_id", "arguments"}
    assert set(choice["arguments"]) == {"listing_id", "quantity"}
    offer = next(
        row for row in known_view["market_offers"]["offers"]
        if row["listing_id"] == choice["arguments"]["listing_id"]
    )
    assert offer["resource_id"] == goal.conditions[0].resource_id
    assert choice["arguments"]["quantity"] == offer["minimum_quantity"]
    assert offer["minimum_within_budget"] is True
    assert "seller_id" not in choice and "stock" not in json.dumps(choice)
    assert "account_id" not in json.dumps(choice)
    system_prompt = models.calls[0][1].messages[0].content
    assert "known_view.preflighted_reserve_purchase_choices" in system_prompt
    assert "不会预留库存" in system_prompt
    assert state_hash(diverse_state) == before

    trade_models = FakeModels({
        "kind": "plan",
        "goal_ids": [choice["goal_id"]],
        "steps": [{
            "capability_id": choice["capability_id"],
            "arguments": choice["arguments"],
        }],
        "reason": "Buy the offered minimum amount for the reserve goal.",
    })
    trade_proposal = await NpcPlanningApplication(trade_models).propose(
        _scope(),
        _world_request(draft, actor),
        diverse_state,
        actor.id,
        state_version=0,
        ruleset_version=draft.ruleset_version,
    )
    assert trade_proposal.status == "ready"
    assert trade_proposal.first_step_preflight is not None
    assert trade_proposal.first_step_preflight.status == "interval_required"
    assert trade_proposal.first_step_preflight.resolution.outcome.code == "trade_completed"
    assert len(trade_models.calls) == 1
    assert state_hash(diverse_state) == before


@pytest.mark.asyncio
async def test_diverse_profile_filters_stale_projected_listing_before_offering_choice(
    feudal_autonomy_states, monkeypatch,
):
    draft, diverse_state, _ = feudal_autonomy_states
    actor, _ = _scheduled_reserve_actor(diverse_state)
    original = npc_planning_module._planning_market_view

    def stale_market_view(state, actor_id, projection):
        view = original(state, actor_id, projection)
        view["offers"] = [
            offer | {"listing_id": str(uuid5(actor.id, f"stale:{offer['listing_id']}"))}
            for offer in view["offers"]
        ]
        return view

    monkeypatch.setattr(npc_planning_module, "_planning_market_view", stale_market_view)
    models = FakeModels({"kind": "defer", "reason": "I will wait."})

    proposal = await NpcPlanningApplication(models).propose(
        _scope(),
        _world_request(draft, actor),
        diverse_state,
        actor.id,
        state_version=0,
        ruleset_version=draft.ruleset_version,
    )

    assert proposal.status == "defer"
    context = json.loads(models.calls[0][1].messages[1].content)
    assert context["known_view"]["preflighted_reserve_purchase_choices"] == []
    system_prompt = models.calls[0][1].messages[0].content
    assert "known_view.preflighted_reserve_purchase_choices" not in system_prompt
    assert "npc_plan_first_step_unavailable" not in system_prompt
    assert "npc_trade_" not in models.calls[0][1].messages[1].content


@pytest.mark.asyncio
async def test_limited_profile_does_not_receive_diverse_purchase_hints(feudal_autonomy_states):
    draft, _, limited_state = feudal_autonomy_states
    assert limited_state.metadata[NPC_AUTONOMY_PROFILE_KEY] == NPC_AUTONOMY_LIMITED_PROFILE
    actor, _ = _scheduled_reserve_actor(limited_state)
    models = FakeModels({"kind": "defer", "reason": "I will wait."})

    proposal = await NpcPlanningApplication(models).propose(
        _scope(),
        _world_request(draft, actor),
        limited_state,
        actor.id,
        state_version=0,
        ruleset_version=draft.ruleset_version,
    )

    assert proposal.status == "defer"
    context = json.loads(models.calls[0][1].messages[1].content)
    assert "preflighted_reserve_purchase_choices" not in context["known_view"]
    assert "preflighted_reserve_purchase_choices" not in models.calls[0][1].messages[0].content


@pytest.mark.asyncio
async def test_diverse_purchase_hint_does_not_force_trade_when_model_selects_valid_route(feudal_autonomy_states):
    draft, diverse_state, _ = feudal_autonomy_states
    actor, goal = _scheduled_reserve_actor(diverse_state)

    def choose_route(request):
        context = json.loads(request.messages[1].content)
        route = next(row for row in context["known_routes"] if row["access_allowed_now"])
        assert context["known_view"]["preflighted_reserve_purchase_choices"]
        return {
            "kind": "plan",
            "goal_ids": [str(goal.id)],
            "steps": [{
                "capability_id": capability_id_for_action("move"),
                "arguments": {
                    "destination_id": route["destination_id"],
                    "route_id": route["route_id"],
                },
            }],
            "reason": "Take an available route before deciding what to do next.",
        }

    models = ContextualFakeModels(choose_route)
    proposal = await NpcPlanningApplication(models).propose(
        _scope(),
        _world_request(draft, actor),
        diverse_state,
        actor.id,
        state_version=0,
        ruleset_version=draft.ruleset_version,
    )

    assert proposal.status == "ready"
    assert proposal.plan is not None
    assert proposal.plan.steps[0].capability_id == capability_id_for_action("move")
    assert proposal.first_step_preflight is not None
    assert proposal.first_step_preflight.status == "journey_start"
    assert len(models.calls) == 1


@pytest.mark.asyncio
async def test_unobserved_private_destination_is_not_prompted_or_accepted(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, goal_id = _with_goal(full_state, actor.id)
    state, hidden_id = _with_hidden_routed_place(state, actor.id)
    models = FakeModels({
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [{"capability_id": capability_id_for_action("move"), "arguments": {"destination_id": str(hidden_id)}}],
        "reason": "Try to travel to an unobserved place.",
    })
    before = state_hash(state)
    proposal = await NpcPlanningApplication(models).propose(_scope(), _request(actor.id), state, actor.id, state_version=7)
    prompt = models.calls[0][1].messages[1].content
    assert proposal.status == "invalid"
    assert proposal.error_code == "invalid_npc_plan_steps"
    assert str(hidden_id) not in prompt
    assert "UNOBSERVED_AUTHOR_ONLY_PLACE_SENTINEL" not in prompt
    assert state_hash(state) == before


@pytest.mark.asyncio
async def test_later_step_may_use_known_destination_without_claiming_it_is_currently_executable(full_state: WorldState):
    actor, destination_id = next(
        (entity, route.to_place_id)
        for entity in full_state.entities.values()
        if entity.kind in {"character", "person"}
        and entity.status == "active"
        and entity.id != full_state.player_id
        and entity.location_id is not None
        for place in [PlaceComponent.model_validate(full_state.entities[entity.location_id].components.get("place"))]
        for route in place.routes
        if full_state.entities[route.to_place_id].components.get("hidden") is not True
    )
    state, goal_id = _with_goal(full_state, actor.id)
    output = {
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [
            {"capability_id": capability_id_for_action("move"), "arguments": {"destination_id": str(destination_id)}},
            {"capability_id": capability_id_for_action("explore"), "arguments": {"place_id": str(destination_id), "focus": "general"}},
        ],
        "reason": "Travel to a known neighboring place and explore after arrival.",
    }
    models = FakeModels(output)
    proposal = await NpcPlanningApplication(models).propose(_scope(), _request(actor.id), state, actor.id, state_version=7)
    assert proposal.status == "ready"
    assert proposal.plan is not None and len(proposal.plan.steps) == 2
    assert proposal.first_step_preflight is not None
    assert proposal.first_step_preflight.status == "journey_start"
    # Only step zero was evaluated against the current location; the second is
    # schema/known-reference reviewed for the future post-arrival context.
    assert proposal.first_step_preflight.action_kind == "move"


@pytest.mark.asyncio
async def test_defer_is_typed_and_provider_failure_propagates(full_state: WorldState):
    actor, _ = _active_actor_with_skill(full_state)
    state, _ = _with_goal(full_state, actor.id)
    deferred = FakeModels({"kind": "defer", "reason": "I need more information."})
    proposal = await NpcPlanningApplication(deferred).propose(_scope(), _request(actor.id), state, actor.id, state_version=7)
    assert proposal.status == "defer"
    assert proposal.plan is None
    assert proposal.defer_reason == "I need more information."

    error = ProviderError(ProviderErrorCode.NETWORK, "provider unavailable")
    broken = FakeModels(error=error)
    with pytest.raises(ProviderError) as caught:
        await NpcPlanningApplication(broken).propose(_scope(), _request(actor.id), state, actor.id, state_version=7)
    assert caught.value is error
    assert len(broken.calls) == 1
    assert state_hash(state) == proposal.state_hash


def test_default_npc_output_schema_resolves_deep_capability_refs():
    goal_id = uuid4()
    schema = _output_schema({goal_id}, build_default_npc_capability_registry())
    Draft202012Validator.check_schema(schema)

    # The sample forces the plan branch and its step alternatives to be
    # traversed. A missing nested capability definition raises during ref
    # resolution, even though this deliberately incomplete step is invalid.
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors({
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [{"capability_id": "probe.capability.v1", "arguments": {}}],
        "reason": "Exercise schema refs.",
    }))
    assert errors


def test_capability_schema_namespaces_duplicate_nested_definitions():
    class SharedPayload(BaseModel):
        value: str

    class FirstIntent(IntentModel):
        kind: Literal["first"] = "first"
        payload: SharedPayload

    class SecondIntent(IntentModel):
        kind: Literal["second"] = "second"
        payload: SharedPayload

    registry = NpcCapabilityRegistry()
    registry.register("probe.first.v1", FirstIntent)
    registry.register("probe.second.v1", SecondIntent)
    goal_id = uuid4()
    schema = _output_schema({goal_id}, registry)
    Draft202012Validator.check_schema(schema)

    first = schema["$defs"]["Capability0Intent"]
    second = schema["$defs"]["Capability1Intent"]
    assert "SharedPayload" in first["$defs"]
    assert "SharedPayload" in second["$defs"]
    first_ref = first["properties"]["payload"]["$ref"]
    second_ref = second["properties"]["payload"]["$ref"]
    assert first_ref == "#/$defs/Capability0Intent/$defs/SharedPayload"
    assert second_ref == "#/$defs/Capability1Intent/$defs/SharedPayload"

    errors = list(Draft202012Validator(schema).iter_errors({
        "kind": "plan",
        "goal_ids": [str(goal_id)],
        "steps": [{"capability_id": "probe.unknown.v1", "arguments": {"payload": {"value": "ok"}}}],
        "reason": "Exercise namespaced schemas.",
    }))
    assert errors


def test_json_pointer_ref_rewrite_preserves_escaped_nested_defs_and_suffixes():
    raw_schema = {
        "type": "object",
        "$defs": {
            "Slash/and~tilde": {
                "type": "object",
                "properties": {
                    "nested": {
                        "type": "object",
                        "$defs": {
                            "Inner/Key~": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        },
                        "properties": {
                            "value": {
                                "$ref": "#/$defs/Slash~1and~0tilde/properties/nested/$defs/Inner~1Key~0/anyOf/0",
                            },
                        },
                    },
                },
            },
        },
        "properties": {
            "nested_value": {
                "$ref": "#/$defs/Slash~1and~0tilde/properties/nested/properties/value",
            },
        },
    }

    namespaced = _rewrite_refs(raw_schema, "#/$defs/Capability7Intent")
    combined = {"$defs": {"Capability7Intent": namespaced}}
    _validate_schema_refs(combined)
    Draft202012Validator.check_schema(combined)
    assert namespaced["properties"]["nested_value"]["$ref"] == (
        "#/$defs/Capability7Intent/$defs/Slash~1and~0tilde/properties/nested/properties/value"
    )
    assert namespaced["$defs"]["Slash/and~tilde"]["properties"]["nested"]["properties"]["value"]["$ref"] == (
        "#/$defs/Capability7Intent/$defs/Slash~1and~0tilde/properties/nested/$defs/Inner~1Key~0/anyOf/0"
    )
    errors = list(Draft202012Validator(combined).iter_errors({"nested_value": "resolved"}))
    assert not errors


def test_schema_ref_composition_rejects_unresolved_or_external_refs():
    with pytest.raises(ValueError, match="non-local"):
        _rewrite_refs({"$ref": "https://example.invalid/schema.json"}, "#/$defs/Capability0Intent")

    with pytest.raises(ValueError, match="unresolved schema reference"):
        _validate_schema_refs({"$defs": {}, "$ref": "#/$defs/missing"})

    with pytest.raises(ValueError, match="malformed JSON Pointer escape"):
        _validate_schema_refs({"$defs": {"bad~2key": {"type": "string"}}, "$ref": "#/$defs/bad~2key"})
