from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.content import ContentCatalog
from world_engine.domain import GoalRecord, ScheduledEventRecord, WorldState
from world_engine.domain.components import PlaceComponent, ProductionComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import EVENT_TYPES, event_payload
from world_engine.domain.models import EntityFieldChangedEvent, TimeAdvancedEvent, WorldEntity
from world_engine.domain.reducers import apply_event
from world_engine.simulation import (
    DecisionResponse,
    LongActionPlan,
    SimulationContext,
    SimulationEngine,
    StopCondition,
    WorldAdvancePlan,
)
from world_engine.simulation.engine import StaleDecisionResponse
from world_engine.rules.contracts import RuleOutcome, RuleResolution


@pytest.fixture(scope="module")
def full_state():
    return ContentCatalog().build_world("modern_city", "Simulation tests", "Mina Sol").state


def _context(
    *, command_id: UUID | None = None, version: int = 0, max_step: int = 86_400,
    responses=(), causal_death_event_ids=None, command_actor_id=None,
) -> SimulationContext:
    return SimulationContext(
        world_id=UUID("10000000-0000-0000-0000-000000000001"),
        branch_id=UUID("20000000-0000-0000-0000-000000000002"),
        command_id=command_id or UUID("30000000-0000-0000-0000-000000000003"),
        world_seed="test-seed",
        state_version=version,
        command_actor_id=command_actor_id,
        ruleset_version="1.0.0",
        max_step_ticks=max_step,
        decision_responses=tuple(responses),
        causal_death_event_ids=causal_death_event_ids or {},
    )


def _with_person(full_state: WorldState, actor_id: UUID, *, life=None, rest_value=None, food_value=None,
                 water_value=None, skills=None, goals=None) -> WorldState:
    raw = full_state.model_dump(mode="json")
    entity = raw["entities"][str(actor_id)]
    if life is not None:
        entity["components"]["life"] = life
    if rest_value is not None:
        entity["components"]["needs"]["rest"]["value"] = rest_value
    if food_value is not None:
        entity["components"]["needs"]["food"]["value"] = food_value
    if water_value is not None:
        entity["components"]["needs"]["water"]["value"] = water_value
    if skills is not None:
        entity["components"]["skills"] = skills
    if goals is not None:
        raw["goals"] = [*raw["goals"], *goals]
    return WorldState.model_validate(raw)


def _with_critical_water(full_state: WorldState, actor_id: UUID) -> WorldState:
    raw = full_state.model_dump(mode="json")
    raw["entities"][str(actor_id)]["components"]["needs"]["water"]["value"] = 20
    for entity_id, entity in raw["entities"].items():
        if entity_id != str(actor_id) and entity["kind"] in {"character", "person"}:
            entity.get("components", {}).pop("needs", None)
    return WorldState.model_validate(raw)


def _atomic_plan(state: WorldState, actor_id: UUID, activity: str, target_tick: int) -> LongActionPlan:
    if activity == "travel":
        actor = state.entities[actor_id]
        origin = state.entities[actor.location_id]
        route = PlaceComponent.model_validate(origin.components["place"]).routes[0]
        return LongActionPlan(
            actor_id=actor_id,
            started_tick=state.current_tick,
            target_tick=target_tick,
            activity="travel",
            route_id=route.route_id,
        )

    producer = next(
        entity for entity in state.entities.values()
        if entity.status == "active" and entity.components.get("production") is not None
    )
    recipe = ProductionComponent.model_validate(producer.components["production"]).recipes[0]
    return LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=target_tick,
        activity="produce",
        producer_id=producer.id,
        recipe_id=recipe.recipe_id,
    )


def _apply_all(state: WorldState, events) -> WorldState:
    projected = state
    for event in events:
        projected = apply_event(projected, event)
    return projected


def test_wait_advances_needs_through_registered_rule_events_without_mutating_input(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    before = full_state.model_dump(mode="json")
    plan = LongActionPlan(actor_id=actor_id, target_tick=2 * full_state.calendar.day_seconds, activity="wait")
    context = _context(max_step=full_state.calendar.day_seconds)

    first = SimulationEngine().plan_window(full_state, plan, context)
    again = SimulationEngine().plan_window(full_state, plan, context)

    assert full_state.model_dump(mode="json") == before
    assert first.outcome == "advanced"
    assert first.next_tick == full_state.calendar.day_seconds
    assert first.metrics.ticks_advanced == full_state.calendar.day_seconds
    assert any(event.type == "core.time_advanced.v1" for event in first.events)
    assert any(event.type == "core.component_replaced.v1" for event in first.events)
    assert tuple(event.model_dump(mode="json") for event in first.events) == tuple(
        event.model_dump(mode="json") for event in again.events
    )
    assert {event.type for event in first.events}.issubset(EVENT_TYPES)

    projected = _apply_all(full_state, first.events)
    assert projected.current_tick == first.next_tick
    assert projected.entities[actor_id].components["needs"]["food"]["value"] < full_state.entities[actor_id].components["needs"]["food"]["value"]


def test_world_advance_applies_needs_to_player_and_npcs_without_a_focal_actor() -> None:
    owner_id, player_id, npc_id = uuid4(), uuid4(), uuid4()
    state = WorldState(
        player_id=player_id,
        calendar={"day_seconds": 60},
        entities={
            entity_id: WorldEntity(
                id=entity_id, kind="character", name=name,
                components={"needs": {"food": {"value": 90}}},
            )
            for entity_id, name in ((player_id, "Player"), (npc_id, "Neighbor"))
        },
    )
    plan = WorldAdvancePlan(started_tick=0, target_tick=60)

    result = SimulationEngine().plan_window(
        state, plan, _context(max_step=60, command_actor_id=owner_id),
    )

    assert result.outcome == "completed"
    assert result.next_tick == 60
    projected = _apply_all(state, result.events)
    assert projected.player_id == player_id
    assert all(projected.entities[entity_id].components["needs"]["food"]["value"] < 90
               for entity_id in (player_id, npc_id))


def test_world_advance_stops_on_exact_need_threshold() -> None:
    owner_id, player_id, npc_id = uuid4(), uuid4(), uuid4()
    state = WorldState(
        player_id=player_id,
        calendar={"day_seconds": 60},
        entities={
            entity_id: WorldEntity(
                id=entity_id, kind="character", name=name,
                components={"needs": {"food": {"value": 100}}},
            )
            for entity_id, name in ((player_id, "Player"), (npc_id, "Neighbor"))
        },
    )
    plan = WorldAdvancePlan(
        started_tick=0, target_tick=60,
        stop_conditions=(StopCondition(
            kind="need_at_or_below", subject_id=player_id, need="food", threshold=90,
        ),),
    )

    result = SimulationEngine().plan_window(
        state, plan, _context(max_step=60, command_actor_id=owner_id),
    )

    assert result.outcome == "stopped"
    assert result.next_tick == 34
    projected = _apply_all(state, result.events)
    assert projected.entities[player_id].components["needs"]["food"]["value"] == 90


def test_world_advance_at_target_commits_lifespan_before_succession_gate() -> None:
    player_id, successor_id, organization_id, owner_id = uuid4(), uuid4(), uuid4(), uuid4()
    state = WorldState(
        player_id=player_id,
        entities={
            player_id: WorldEntity(
                id=player_id, kind="character", name="Player Leader",
                components={"life": {"birth_tick": 0, "lifespan_ticks": 60}},
            ),
            successor_id: WorldEntity(id=successor_id, kind="character", name="Aster"),
            organization_id: WorldEntity(
                id=organization_id, kind="organization", name="Council",
                components={"governance": {
                    "members": [str(player_id), str(successor_id)],
                    "leader_id": str(player_id),
                    "succession_policy": "election",
                }},
            ),
        },
    )
    plan = WorldAdvancePlan(started_tick=0, target_tick=60)
    engine = SimulationEngine()

    expired = engine.plan_window(
        state, plan, _context(max_step=60, command_actor_id=owner_id),
    )

    assert expired.outcome == "advanced"
    assert expired.next_tick == 60
    assert any(event.type == "core.life_ended.v1" for event in expired.events)
    after_death = _apply_all(state, expired.events)
    assert after_death.player_id == player_id
    assert after_death.entities[player_id].status == "dead"
    gate_context = _context(version=1, max_step=60, command_actor_id=owner_id, command_id=uuid4())
    gate = engine.plan_window(after_death, plan, gate_context)
    assert gate.outcome == "decision_required"
    assert gate.decision_requests[0].kind == "organization.succession"
    assert gate.decision_requests[0].decision_owner == "model"


def test_rest_action_stops_exactly_when_the_rest_need_reaches_threshold(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_person(full_state, actor_id, rest_value=60)
    plan = LongActionPlan(
        actor_id=actor_id,
        target_tick=full_state.calendar.day_seconds,
        activity="rest",
        stop_conditions=(StopCondition(kind="need_at_or_above", subject_id=actor_id, need="rest", threshold=80),),
    )

    resolution = SimulationEngine().plan_window(state, plan, _context(max_step=full_state.calendar.day_seconds))

    assert resolution.outcome == "stopped"
    assert resolution.interruption is not None and resolution.interruption.reason_code == "stop_condition_reached"
    assert resolution.next_tick == 20 * full_state.calendar.day_seconds // 36
    projected = _apply_all(state, resolution.events)
    assert projected.entities[actor_id].components["needs"]["rest"]["value"] == 80


def test_scheduled_event_is_a_hard_boundary_with_passive_needs_applied(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    due_tick = 1800
    scheduled = ScheduledEventRecord(
        id=uuid4(),
        due_tick=due_tick,
        phase="environment",
        priority=0,
        handler_id="test.unhandled_event",
        handler_version="1.0.0",
        status="queued",
    )
    raw = full_state.model_dump(mode="json")
    raw["scheduled_events"] = [scheduled.model_dump(mode="json")]
    state = WorldState.model_validate(raw)
    plan = LongActionPlan(actor_id=actor_id, target_tick=3600, activity="wait")

    resolution = SimulationEngine().plan_window(state, plan, _context(max_step=3600))

    assert resolution.outcome == "rule_required"
    assert resolution.next_tick == due_tick
    assert resolution.interruption is not None
    assert resolution.interruption.reference_id == scheduled.id
    assert any(event.type == "core.component_replaced.v1" for event in resolution.events)
    assert _apply_all(state, resolution.events).current_tick == due_tick


def test_early_rule_resolution_keeps_its_actual_clock_delta(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    scheduled = ScheduledEventRecord(
        id=uuid4(),
        due_tick=3600,
        phase="environment",
        handler_id="test.unhandled_event",
        handler_version="1.0.0",
        status="queued",
    )
    raw = full_state.model_dump(mode="json")
    raw["scheduled_events"] = [scheduled.model_dump(mode="json")]
    state = WorldState.model_validate(raw)

    class EarlyRuleEvaluator:
        def evaluate(self, current_state, intent, rule_context):
            return RuleResolution(
                events=[TimeAdvancedEvent(to_tick=current_state.current_tick + 60)],
                outcome=RuleOutcome(status="partial", code="test_early_stop", message="early boundary"),
                elapsed_ticks=60,
                ruleset_version=rule_context.ruleset_version,
            )

    resolution = SimulationEngine(rule_engine=EarlyRuleEvaluator()).plan_window(
        state,
        LongActionPlan(actor_id=actor_id, target_tick=7200, activity="wait"),
        _context(max_step=7200),
    )

    assert resolution.outcome == "stopped"
    assert resolution.next_tick == 60
    assert resolution.metrics.ticks_advanced == 60
    assert _apply_all(state, resolution.events).current_tick == 60


def test_overdue_scheduled_event_pauses_for_explicit_reconciliation(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    scheduled = ScheduledEventRecord(
        id=uuid4(),
        due_tick=0,
        phase="environment",
        handler_id="production.quota_renewal",
        handler_version="1.0.0",
        status="queued",
        payload={"producer_id": str(actor_id), "quota_id": "quota.test"},
    )
    raw = full_state.model_dump(mode="json")
    raw["current_tick"] = 3600
    raw["scheduled_events"] = [scheduled.model_dump(mode="json")]
    state = WorldState.model_validate(raw)

    resolution = SimulationEngine().plan_window(
        state,
        LongActionPlan(actor_id=actor_id, target_tick=7200, activity="wait"),
        _context(),
    )

    assert resolution.outcome == "rule_required"
    assert resolution.next_tick == 3600
    assert resolution.events == ()
    assert resolution.interruption is not None
    assert resolution.interruption.reason_code == "scheduled_event_overdue_reconciliation_required"


def test_population_year_schedule_advances_every_seeded_age_cohort(full_state: WorldState) -> None:
    year_seconds = full_state.calendar.year_days * full_state.calendar.day_seconds
    raw = full_state.model_dump(mode="json")
    raw["current_tick"] = year_seconds
    state = WorldState.model_validate(raw)
    assert state.player_id is not None

    resolution = SimulationEngine().plan_window(
        state,
        LongActionPlan(actor_id=state.player_id, target_tick=2 * year_seconds, activity="wait"),
        _context(max_step=year_seconds),
    )

    assert resolution.outcome == "advanced"
    assert resolution.next_tick == year_seconds
    assert [event.type for event in resolution.events] == [
        "core.population_year_advanced.v1",
        "core.scheduled_event_completed.v1",
    ]
    projected = _apply_all(state, resolution.events)
    assert all(
        entity.components["population"]["last_advanced_year"] == 1
        for entity in projected.entities.values()
        if "age_cohorts" in entity.components.get("population", {})
    )
    schedule = projected.scheduled_events[0]
    assert schedule.status == "queued"
    assert schedule.completed_runs == 1
    assert schedule.due_tick == 2 * year_seconds


def test_npc_lifespan_boundary_commits_all_deaths_then_requests_succession(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    characters = [
        entity for entity in full_state.entities.values()
        if entity.kind in {"character", "person"} and entity.status not in {"dead", "retired"}
    ]
    characters.sort(key=lambda entity: str(entity.id))
    other_expiring = next(entity for entity in characters if entity.id != actor_id)
    second_expiring = next(entity for entity in characters if entity.id not in {actor_id, other_expiring.id})
    successor = next(
        entity for entity in characters
        if entity.id not in {actor_id, other_expiring.id, second_expiring.id}
    )
    organization = next(entity for entity in full_state.entities.values() if entity.kind == "organization")
    raw = full_state.model_dump(mode="json")
    for expiring_id in (other_expiring.id, second_expiring.id):
        life = dict(raw["entities"][str(expiring_id)]["components"]["life"])
        life.update({"birth_tick": 0, "lifespan_ticks": 3600, "is_immortal": False})
        raw["entities"][str(expiring_id)]["components"]["life"] = life
    raw["entities"][str(organization.id)]["components"]["governance"] = {
        "members": [str(actor_id), str(other_expiring.id), str(second_expiring.id), str(successor.id)],
        "leader_id": str(other_expiring.id),
        "offices": [],
        "succession_policy": "election",
        "decision_threshold": "0.5",
        "authority_tags": [],
    }
    state = WorldState.model_validate(raw)
    plan = LongActionPlan(actor_id=actor_id, target_tick=7200, activity="wait")

    resolution = SimulationEngine().plan_window(state, plan, _context(max_step=7200))

    assert resolution.outcome == "advanced"
    assert resolution.next_tick == 3600
    assert resolution.interruption is None
    life_events = [event for event in resolution.events if event.type == "core.life_ended.v1"]
    assert {event.entity_id for event in life_events} == {other_expiring.id, second_expiring.id}
    projected = _apply_all(state, resolution.events)
    assert projected.entities[actor_id].status == "active"
    assert projected.entities[other_expiring.id].status == "dead"
    assert projected.entities[second_expiring.id].status == "dead"

    succession = SimulationEngine().plan_window(
        projected, plan, _context(version=1, max_step=7200)
    )
    assert succession.outcome == "decision_required"
    request = succession.decision_requests[0]
    assert request.kind == "organization.succession"
    assert request.decision_owner == "user"
    assert request.at_tick == projected.current_tick
    assert any(successor.name in option.label for option in request.options)


def test_overdue_lifespan_is_resolved_immediately_instead_of_leaving_npc_active(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    raw = full_state.model_dump(mode="json")
    raw["current_tick"] = 4000
    life = dict(raw["entities"][str(actor_id)]["components"]["life"])
    life.update({"birth_tick": 0, "lifespan_ticks": 3600, "is_immortal": False})
    raw["entities"][str(actor_id)]["components"]["life"] = life
    state = WorldState.model_validate(raw)

    resolution = SimulationEngine().plan_window(
        state,
        LongActionPlan(actor_id=actor_id, target_tick=5000, activity="wait"),
        _context(version=5),
    )

    assert resolution.outcome == "stopped"
    assert resolution.next_tick == 4000
    assert [event.type for event in resolution.events] == ["core.life_ended.v1"]
    assert resolution.interruption is not None
    assert resolution.interruption.reason_code == "actor_lifespan_expired"
    assert _apply_all(state, resolution.events).entities[actor_id].status == "dead"


def test_same_tick_earlier_decision_phase_precedes_lifespan_closure(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    due_tick = 3600
    scheduled = ScheduledEventRecord(
        id=uuid4(),
        due_tick=due_tick,
        phase="decision",
        priority=0,
        handler_id="test.decision",
        handler_version="1.0.0",
        status="queued",
        payload={"decision_options": [{"option_id": "resolve", "label": "Resolve boundary"}]},
    )
    raw = full_state.model_dump(mode="json")
    life = dict(raw["entities"][str(actor_id)]["components"]["life"])
    life.update({"birth_tick": 0, "lifespan_ticks": due_tick, "is_immortal": False})
    raw["entities"][str(actor_id)]["components"]["life"] = life
    raw["scheduled_events"] = [scheduled.model_dump(mode="json")]
    state = WorldState.model_validate(raw)
    plan = LongActionPlan(actor_id=actor_id, target_tick=7200, activity="wait")

    boundary = SimulationEngine().plan_window(state, plan, _context(max_step=7200))
    projected = _apply_all(state, boundary.events)
    assert boundary.outcome == "advanced"
    assert boundary.next_tick == due_tick
    assert not any(event.type == "core.life_ended.v1" for event in boundary.events)
    assert projected.entities[actor_id].status == "active"

    decision = SimulationEngine().plan_window(
        projected, plan, _context(version=1, max_step=7200)
    )
    assert decision.outcome == "decision_required"
    assert decision.decision_requests[0].evidence_ids == (scheduled.id,)


@pytest.mark.parametrize("leave_vacant", [False, True])
def test_elected_member_gets_user_owned_public_succession_options(full_state: WorldState, leave_vacant: bool) -> None:
    player_id = full_state.player_id
    assert player_id is not None
    existing_officeholders = set()
    for entity in full_state.entities.values():
        governance = entity.components.get("governance")
        if isinstance(governance, dict):
            existing_officeholders.add(governance.get("leader_id"))
            existing_officeholders.update(office["holder_id"] for office in governance["offices"])
    deceased = next(
        entity for entity in full_state.entities.values()
        if entity.kind in {"character", "person"}
        and entity.id != player_id
        and str(entity.id) not in existing_officeholders
        and entity.status not in {"dead", "retired"}
    )
    raw = full_state.model_dump(mode="json")
    organization = next(entity for entity in full_state.entities.values() if entity.kind == "organization")
    raw["entities"][str(organization.id)]["components"]["governance"] = {
        "members": [str(player_id), str(deceased.id)],
        "leader_id": str(deceased.id),
        "offices": [
            {"office_id": "office.leader", "title": "Leader", "holder_id": str(deceased.id)},
            {"office_id": "office.stock", "title": "Stock clerk", "holder_id": str(deceased.id)},
        ],
        "succession_policy": "election",
        "decision_threshold": "0.5",
        "authority_tags": [],
    }
    state = WorldState.model_validate(raw)
    cohort_id = UUID(deceased.components["life"]["population_group_id"])
    death = EntityFieldChangedEvent(
        entity_id=deceased.id, field_name="status", value="dead",
        expected_population_revision=state.entities[cohort_id].components["population"]["revision"],
    )
    state = apply_event(state, death)
    plan = LongActionPlan(actor_id=player_id, target_tick=3600, activity="wait")
    engine = SimulationEngine()

    waiting = engine.plan_window(state, plan, _context())

    request = waiting.decision_requests[0]
    assert waiting.outcome == "decision_required"
    assert request.decision_owner == "user"
    assert request.evidence_ids == ()
    assert all(str(player_id) not in option.option_id for option in request.options)
    assert any(option.label == f"Appoint {full_state.entities[player_id].name}" for option in request.options)

    response = DecisionResponse(
        request_id=request.request_id,
        state_version=request.state_version,
        at_tick=request.at_tick,
        option_id="leave_vacant" if leave_vacant else next(
            option.option_id for option in request.options if option.option_id != "leave_vacant"
        ),
        source="model",
    )
    with pytest.raises(StaleDecisionResponse):
        engine.plan_window(state, plan, _context(responses=(response,)))

    user_response = response.model_copy(update={"source": "user"})
    accepted = engine.plan_window(
        state, plan,
        _context(
            responses=(user_response,),
            causal_death_event_ids={deceased.id: death.event_id},
        ),
    )
    assert accepted.outcome == "advanced"
    assert accepted.next_tick == state.current_tick
    assert [event.type for event in accepted.events] == ["core.governance_succession.v1"]
    assert accepted.accepted_decisions == (user_response,)
    assert "resolution_target" not in request.model_dump(mode="json")
    event, = accepted.events
    assert event.linked_leadership_office_ids == ["office.leader"]
    projected = _apply_all(state, accepted.events)
    governance = projected.entities[organization.id].components["governance"]
    successor = None if leave_vacant else str(player_id)
    assert governance["leader_id"] == successor
    assert {office["office_id"]: office["holder_id"] for office in governance["offices"]} == {
        "office.leader": successor, "office.stock": str(deceased.id),
    }
    next_request, = engine.plan_window(projected, plan, _context(version=1)).decision_requests
    assert next_request.resolution_target.office_id == "office.stock"

    with pytest.raises(InvalidEvent, match="exactly the predecessor"):
        apply_event(state, event.model_copy(update={"linked_leadership_office_ids": []}))

    legacy = event.model_copy(update={"linked_leadership_office_ids": None})
    assert "linked_leadership_office_ids" not in event_payload(legacy)
    old_governance = apply_event(state, legacy).entities[organization.id].components["governance"]
    assert old_governance["leader_id"] == successor
    assert old_governance["offices"][0]["holder_id"] == str(deceased.id)


def test_equal_priority_goals_request_a_fresh_bound_npc_decision(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    shared = {"owner_id": str(actor_id), "priority": 100, "created_tick": 0, "updated_tick": 0}
    goals = [
        GoalRecord(id=uuid4(), title="Help the family", **shared).model_dump(mode="json"),
        GoalRecord(id=uuid4(), title="Keep the clinic open", **shared).model_dump(mode="json"),
    ]
    state = _with_person(full_state, actor_id, goals=goals)
    plan = LongActionPlan(actor_id=actor_id, target_tick=7200, activity="study", skill_id="skill.care")
    engine = SimulationEngine()
    context = _context()

    request = engine.plan_window(state, plan, context)
    assert request.outcome == "decision_required"
    assert request.events == ()
    assert len(request.decision_requests) == 1
    decision = request.decision_requests[0]
    assert decision.state_version == context.state_version
    assert decision.at_tick == state.current_tick
    assert {"Help the family", "Keep the clinic open"}.issubset({item.label for item in decision.options})

    response = DecisionResponse(
        request_id=decision.request_id,
        state_version=context.state_version,
        at_tick=state.current_tick,
        option_id=decision.options[0].option_id,
    )
    accepted = engine.plan_window(state, plan, _context(responses=(response,)))
    assert accepted.outcome == "rule_required"
    assert accepted.events == ()
    assert accepted.accepted_decisions == (response,)

    stale = response.model_copy(update={"state_version": context.state_version + 1})
    with pytest.raises(StaleDecisionResponse):
        engine.plan_window(state, plan, _context(responses=(stale,)))


def test_plan_rejects_unknown_constraint_instead_of_ignoring_it(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    with pytest.raises(ValidationError):
        LongActionPlan(actor_id=actor_id, target_tick=1000, activity="wait", constraints=("skip_death",))


@pytest.mark.parametrize("activity", ("travel", "produce"))
def test_incidental_water_break_does_not_complete_atomic_action_plan(
    full_state: WorldState,
    activity: str,
) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_critical_water(full_state, actor_id)
    plan = _atomic_plan(state, actor_id, activity, state.current_tick + 3600)

    resolution = SimulationEngine().plan_window(state, plan, _context(max_step=3600))

    assert resolution.outcome == "advanced"
    assert resolution.next_tick == state.current_tick + 60
    assert resolution.interruption is not None
    assert resolution.interruption.reason_code == "incidental_care_completed"
    assert resolution.continuation == plan
    assert resolution.rule_resolution is not None
    assert resolution.rule_resolution.outcome.code == "consumed"
    assert resolution.rule_resolution.outcome.details["need"] == "water"
    projected = _apply_all(state, resolution.events)
    assert projected.entities[actor_id].location_id == state.entities[actor_id].location_id
    assert all(event.type != "core.movement_completed.v1" for event in resolution.events)


@pytest.mark.parametrize("activity", ("travel", "produce"))
def test_atomic_plan_keeps_not_started_reason_when_incidental_care_reaches_target(
    full_state: WorldState,
    activity: str,
) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_critical_water(full_state, actor_id)
    plan = _atomic_plan(state, actor_id, activity, state.current_tick + 60)
    engine = SimulationEngine()

    resolution = engine.plan_window(state, plan, _context(max_step=3600))

    expected_reason = f"{activity}_not_started_before_target"
    assert resolution.outcome == "rule_required"
    assert resolution.next_tick == plan.target_tick
    assert resolution.interruption is not None
    assert resolution.interruption.reason_code == expected_reason
    assert resolution.rule_resolution is not None
    assert resolution.rule_resolution.outcome.code == "consumed"
    projected = _apply_all(state, resolution.events)
    assert projected.entities[actor_id].location_id == state.entities[actor_id].location_id

    resumed_at_target = engine.plan_window(
        projected,
        plan,
        _context(version=1, max_step=3600),
    )
    assert resumed_at_target.outcome == "rule_required"
    assert resumed_at_target.interruption is not None
    assert resumed_at_target.interruption.reason_code == expected_reason


def test_eating_uses_fifo_lots_and_restores_food_need(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_person(full_state, actor_id, food_value=40)
    food_resource = state.metadata["resource_roles"]["food_resource_id"]
    before_balance = next(
        account.balance for account in state.resources.values()
        if account.owner_id == actor_id and account.resource_id == food_resource
    )
    before_need = state.entities[actor_id].components["needs"]["food"]["value"]
    plan = LongActionPlan(actor_id=actor_id, target_tick=300, activity="eat")

    resolution = SimulationEngine().plan_window(state, plan, _context())

    assert resolution.outcome == "completed"
    assert resolution.next_tick == 300
    assert resolution.metrics.resources_consumed == ((food_resource, "1"),)
    projected = _apply_all(state, resolution.events)
    after_balance = next(
        account.balance for account in projected.resources.values()
        if account.owner_id == actor_id and account.resource_id == food_resource
    )
    assert before_balance - after_balance == 1
    assert projected.entities[actor_id].components["needs"]["food"]["value"] > before_need


def test_study_uses_registered_training_rule_and_caps_to_context_step(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    actor = full_state.entities[actor_id]
    raw_skills = dict(actor.components["skills"])
    skill_map = dict(raw_skills["skills"])
    old_skill = dict(skill_map["skill.local_knowledge"])
    old_experience = old_skill["experience"]
    plan = LongActionPlan(actor_id=actor_id, target_tick=7200, activity="study", skill_id="skill.local_knowledge")

    resolution = SimulationEngine().plan_window(
        full_state,
        plan,
        _context(max_step=3600),
    )

    assert resolution.outcome == "advanced"
    assert resolution.next_tick == 3600
    projected = _apply_all(full_state, resolution.events)
    new_skill = projected.entities[actor_id].components["skills"]["skills"]["skill.local_knowledge"]
    assert new_skill["experience"] == old_experience + 12
    assert new_skill["trained_ticks"] == old_skill["trained_ticks"] + 3600


def test_incidental_drink_does_not_finish_study_and_training_resumes(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_critical_water(full_state, actor_id)
    old_skill = state.entities[actor_id].components["skills"]["skills"]["skill.local_knowledge"]
    plan = LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=state.current_tick + 7200,
        activity="study",
        skill_id="skill.local_knowledge",
    )
    engine = SimulationEngine()

    care = engine.plan_window(state, plan, _context(max_step=3600))

    assert care.outcome == "advanced"
    assert care.next_tick == state.current_tick + 60
    assert care.continuation == plan
    assert care.rule_resolution is not None
    assert care.rule_resolution.outcome.details["need"] == "water"
    after_care = _apply_all(state, care.events)
    care_skill = after_care.entities[actor_id].components["skills"]["skills"]["skill.local_knowledge"]
    assert care_skill["trained_ticks"] == old_skill["trained_ticks"]

    training = engine.plan_window(
        after_care,
        care.continuation,
        _context(version=1, max_step=3600),
    )

    assert training.outcome == "advanced"
    assert training.next_tick == after_care.current_tick + 3600
    after_training = _apply_all(after_care, training.events)
    trained_skill = after_training.entities[actor_id].components["skills"]["skills"]["skill.local_knowledge"]
    assert trained_skill["trained_ticks"] == old_skill["trained_ticks"] + 3600


def test_study_completes_at_its_finite_target(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    old_skill = full_state.entities[actor_id].components["skills"]["skills"]["skill.local_knowledge"]
    plan = LongActionPlan(
        actor_id=actor_id,
        started_tick=full_state.current_tick,
        target_tick=full_state.current_tick + 7200,
        activity="study",
        skill_id="skill.local_knowledge",
    )
    engine = SimulationEngine()

    first = engine.plan_window(full_state, plan, _context(max_step=3600))
    assert first.outcome == "advanced"
    assert first.next_tick == full_state.current_tick + 3600
    after_first = _apply_all(full_state, first.events)

    final = engine.plan_window(
        after_first,
        first.continuation,
        _context(version=1, max_step=3600),
    )

    assert final.outcome == "completed"
    assert final.next_tick == plan.target_tick
    assert final.continuation is None
    assert final.metrics.integration_boundary == "target_reached"
    completed = _apply_all(after_first, final.events)
    skill = completed.entities[actor_id].components["skills"]["skills"]["skill.local_knowledge"]
    assert skill["trained_ticks"] == old_skill["trained_ticks"] + 7200


@pytest.mark.parametrize("activity", ("study", "rest", "wait"))
def test_adaptive_action_runs_to_exact_stocked_food_or_water_threshold(
    full_state: WorldState,
    activity: str,
) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_critical_water(full_state, actor_id)
    raw = state.model_dump(mode="json")
    needs = raw["entities"][str(actor_id)]["components"]["needs"]
    needs["food"]["value"] = 100
    needs["water"]["value"] = 21
    needs["water"]["decay_remainder"] = 30
    needs["rest"]["value"] = 100
    state = WorldState.model_validate(raw)
    start_tick = state.current_tick
    expected_threshold_tick = start_tick + 2879
    plan_fields = {
        "actor_id": actor_id,
        "started_tick": start_tick,
        "target_tick": start_tick + 2 * state.calendar.day_seconds,
        "activity": activity,
    }
    if activity == "study":
        plan_fields["skill_id"] = "skill.local_knowledge"
    plan = LongActionPlan(**plan_fields)
    engine = SimulationEngine()

    first = engine.plan_window(state, plan, _context(max_step=state.calendar.day_seconds))

    assert first.outcome == "advanced"
    assert first.next_tick == expected_threshold_tick
    assert first.metrics.integration_boundary == "actor_self_care_threshold"
    assert first.interruption is not None
    assert first.interruption.reason_code == "actor_self_care_threshold"
    assert first.metrics.resources_consumed == ()
    after_threshold = _apply_all(state, first.events)
    assert after_threshold.entities[actor_id].components["needs"]["water"]["value"] == 20

    care = engine.plan_window(
        after_threshold,
        first.continuation,
        _context(version=1, max_step=state.calendar.day_seconds),
    )

    assert care.outcome == "advanced"
    assert care.next_tick == expected_threshold_tick + 60
    assert care.rule_resolution is not None
    assert care.rule_resolution.outcome.details["need"] == "water"
    assert care.metrics.resources_consumed


def test_subminute_incidental_rest_uses_bounded_wait_before_stocked_care(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_critical_water(full_state, actor_id)
    raw = state.model_dump(mode="json")
    needs = raw["entities"][str(actor_id)]["components"]["needs"]
    needs["food"]["value"] = 100
    needs["water"]["value"] = 21
    needs["water"]["decay_remainder"] = state.calendar.day_seconds - 30 * 30
    needs["rest"]["value"] = 20
    state = WorldState.model_validate(raw)
    plan = LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=state.current_tick + state.calendar.day_seconds,
        activity="study",
        skill_id="skill.local_knowledge",
    )

    first = SimulationEngine().plan_window(state, plan, _context(max_step=state.calendar.day_seconds))

    assert first.outcome == "advanced"
    assert first.next_tick == state.current_tick + 30
    assert first.metrics.integration_boundary == "actor_self_care_threshold"
    after_threshold = _apply_all(state, first.events)
    assert after_threshold.entities[actor_id].components["needs"]["water"]["value"] == 20

    care = SimulationEngine().plan_window(
        after_threshold,
        first.continuation,
        _context(version=1, max_step=state.calendar.day_seconds),
    )
    assert care.rule_resolution is not None
    assert care.rule_resolution.outcome.details["need"] == "water"


def test_no_stock_does_not_preempt_real_survival_pressure_during_study(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_critical_water(full_state, actor_id)
    raw = state.model_dump(mode="json")
    roles = raw["metadata"]["resource_roles"]
    water_resource = roles["water_resource_id"]
    actor = raw["entities"][str(actor_id)]
    water_account_ids = {
        lot["account_id"] for lot in actor["components"]["inventory"]["lots"]
        if lot["resource_id"] == water_resource
    }
    actor["components"]["inventory"]["lots"] = [
        lot for lot in actor["components"]["inventory"]["lots"]
        if lot["resource_id"] != water_resource
    ]
    for account_id in water_account_ids:
        raw["resources"][account_id]["balance"] = "0"
    needs = actor["components"]["needs"]
    needs["food"]["value"] = 100
    needs["water"]["value"] = 20
    needs["water"]["last_critical_damage_tick"] = -state.calendar.day_seconds
    needs["rest"]["value"] = 100
    state = WorldState.model_validate(raw)
    plan = LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=state.current_tick + 3600,
        activity="study",
        skill_id="skill.local_knowledge",
    )

    resolution = SimulationEngine().plan_window(state, plan, _context(max_step=3600))

    assert resolution.outcome == "stopped"
    assert resolution.next_tick == state.current_tick + 3600
    assert resolution.rule_resolution is not None
    assert resolution.rule_resolution.outcome.code == "survival_pressure"
    assert not resolution.metrics.resources_consumed


def test_incidental_drink_near_target_waits_only_to_target_and_keeps_survival_stop(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_critical_water(full_state, actor_id)
    raw = state.model_dump(mode="json")
    water = raw["entities"][str(actor_id)]["components"]["needs"]["water"]
    water["value"] = 0
    water["last_critical_damage_tick"] = -state.calendar.day_seconds
    state = WorldState.model_validate(raw)
    plan = LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=state.current_tick + 30,
        activity="study",
        skill_id="skill.local_knowledge",
    )

    resolution = SimulationEngine().plan_window(state, plan, _context(max_step=3600))

    assert resolution.outcome == "stopped"
    assert resolution.next_tick == plan.target_tick
    assert resolution.rule_resolution is not None
    assert resolution.rule_resolution.outcome.code == "survival_pressure"
    assert resolution.interruption is not None
    assert resolution.interruption.reason_code == "survival_pressure"


def test_production_uses_finite_site_quota_and_consumes_inventory(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    producer_id = full_state.entities[actor_id].location_id
    assert producer_id is not None
    producer = full_state.entities[producer_id]
    recipe = producer.components["production"]["recipes"][0]
    input_resource = recipe["inputs"][0]["resource_id"]
    output_resource = recipe["outputs"][0]["resource_id"]
    raw_actor = dict(full_state.entities[actor_id].components)
    raw_skills = dict(raw_actor["skills"])
    skill_map = dict(raw_skills["skills"])
    local = dict(skill_map["skill.local_knowledge"])
    local["level"] = "20"
    skill_map["skill.local_knowledge"] = local
    raw_skills["skills"] = skill_map
    state = _with_person(full_state, actor_id, skills=raw_skills)
    site = state.entities[producer_id]
    source = next(
        account for account in state.resources.values()
        if account.account_type == "production_source" and account.owner_id == producer_id and account.resource_id == output_resource
    )
    input_holder = next(
        account for account in state.resources.values()
        if account.owner_id == producer_id and account.resource_id == input_resource
    )
    source_before, input_before = source.balance, input_holder.balance
    plan = LongActionPlan(
        actor_id=actor_id,
        target_tick=recipe["duration_ticks"],
        activity="produce",
        producer_id=producer_id,
        recipe_id=recipe["recipe_id"],
    )

    resolution = SimulationEngine().plan_window(state, plan, _context(max_step=86_400))

    assert resolution.outcome == "completed"
    assert resolution.next_tick == recipe["duration_ticks"]
    assert resolution.metrics.resources_consumed == tuple(
        (flow["resource_id"], flow["quantity"]) for flow in recipe["inputs"]
    )
    projected = _apply_all(state, resolution.events)
    source_after = projected.resources[next(
        account_id for account_id, account in state.resources.items()
        if account.account_type == "production_source" and account.owner_id == producer_id and account.resource_id == output_resource
    )]
    input_after = projected.resources[next(
        account_id for account_id, account in state.resources.items()
        if account.owner_id == producer_id and account.resource_id == input_resource
    )]
    assert source_before - source_after.balance == 1
    assert input_before - input_after.balance == 2
    assert source_after.balance >= -source_after.credit_limit
    assert site.components["production"]["recipes"][0]["recipe_id"] == recipe["recipe_id"]
