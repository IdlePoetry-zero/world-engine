from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.content import ContentCatalog
from world_engine.domain.components import (
    GoalRecord,
    GovernanceComponent,
    OfficeRecord,
    PlaceComponent,
    ScheduledEventRecord,
    SkillsComponent,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.leadership_ballot_state import validate_leadership_ballot_batch
from world_engine.domain.leadership_ballots import open_leadership_ballot
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.npc_plan_events import NpcPlanCreatedEvent, npc_plan_event_id
from world_engine.domain.npc_plan_review import (
    build_npc_plan_review_schedule,
    npc_plan_review_request_id,
)
from world_engine.domain.npc_plan_review_events import (
    NpcPlanReviewResolvedEvent,
    npc_plan_review_event_id,
)
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.relationship_invitation_state import validate_relationship_invitation_batch
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.simulation.contracts import (
    DecisionResponse,
    LongActionPlan,
    SimulationContext,
    WorldAdvancePlan,
)
from world_engine.simulation.engine import SimulationEngine
from world_engine.simulation.npc_capabilities import capability_id_for_action
from world_engine.simulation.npc_plan_execution import build_npc_plan_execution_transition
from world_engine.simulation.npc_plan_validation import validate_npc_plan_simulation_batch


@pytest.fixture(scope="module")
def catalog_world():
    draft = ContentCatalog().build_world("modern_city", "NPC plan validation tests", "Test Player", seed=204)
    return draft, draft.state


def _active_npc_with_goal(state: WorldState):
    for goal in state.goals:
        actor = state.entities.get(goal.owner_id)
        skills = (
            SkillsComponent.model_validate(actor.components.get("skills", {})).skills
            if actor is not None else {}
        )
        if (
            actor is not None
            and actor.kind in {"character", "person"}
            and actor.status == "active"
            and actor.id != state.player_id
            and goal.status == "active"
            and goal.created_tick <= state.current_tick
            and (goal.deadline is None or goal.deadline >= state.current_tick)
            and bool(skills)
            and not any(plan.actor_id == actor.id and plan.status in {"ready", "running"} for plan in state.npc_plans)
        ):
            return actor, goal
    raise AssertionError("modern catalog requires an active unplanned NPC with an owned active goal")


def _context(draft, *, actor_id: UUID | None = None, state_version: int = 12, externally_controlled=()):
    return SimulationContext(
        world_id=draft.world_id,
        branch_id=draft.branch_id,
        command_id=uuid4(),
        command_actor_id=actor_id,
        externally_controlled_actor_ids=frozenset(externally_controlled),
        world_seed="npc-plan-validation-seed",
        state_version=state_version,
        ruleset_version=draft.ruleset_version,
    )


def _simulation_plan(actor_id: UUID, tick: int) -> LongActionPlan:
    return LongActionPlan(actor_id=actor_id, started_tick=tick, target_tick=tick, activity="wait")


def _with_review_schedule(state: WorldState, draft, actor_id: UUID) -> WorldState:
    schedule = build_npc_plan_review_schedule(state, draft.world_id, actor_id, state.current_tick)
    return state.model_copy(update={"scheduled_events": [*state.scheduled_events, schedule]})


def _review_events(state: WorldState, plan, context, *, source: str = "model", outcome: str = "ready"):
    probe_context = context.model_copy(update={"decision_responses": ()})
    probe = SimulationEngine().plan_window(state, plan, probe_context)
    assert probe.outcome == "decision_required"
    assert len(probe.decision_requests) == 1
    request = probe.decision_requests[0]
    proposed_plan = None
    if outcome == "ready":
        actor = state.entities[request.actor_id]
        skill_id = min(SkillsComponent.model_validate(actor.components.get("skills", {})).skills)
        goal = next(goal for goal in state.goals if goal.owner_id == request.actor_id and goal.status == "active")
        proposed_plan = NpcPlan(
            plan_id=uuid5(request.request_id, f"npc.plan.v1:{request.actor_id}:{context.state_version}"),
            actor_id=request.actor_id,
            goal_ids=[goal.id],
            revision=1,
            status="ready",
            created_tick=state.current_tick,
            updated_tick=state.current_tick,
            next_review_tick=state.current_tick,
            steps=[NpcPlanStep(
                capability_id=capability_id_for_action("train"),
                arguments={"skill_id": skill_id, "duration_ticks": 600},
            )],
            current_step=0,
            reason="A bounded actor proposal.",
        )
    response = DecisionResponse(
        request_id=request.request_id,
        state_version=context.state_version,
        at_tick=state.current_tick,
        source=source,
        option_id="plan",
        explanation="npc_planning_" + outcome,
        npc_planning_status=outcome,
        npc_plan=proposed_plan,
        npc_plan_state_hash=state_hash(state),
        npc_model_call_id="trace-validation-1" if source == "model" else None,
    )
    resolution = SimulationEngine().plan_window(
        state,
        plan,
        probe_context.model_copy(update={"decision_responses": (response,)}),
    )
    return resolution.events


def _blocked_plan(actor_id: UUID, goal_id: UUID, tick: int) -> NpcPlan:
    return NpcPlan(
        plan_id=uuid4(),
        actor_id=actor_id,
        goal_ids=[goal_id],
        revision=1,
        status="blocked",
        created_tick=tick,
        updated_tick=tick,
        next_review_tick=tick,
        steps=[NpcPlanStep(capability_id=capability_id_for_action("explore"), arguments={})],
        current_step=0,
        attempts=1,
        last_failure="A prior registered action was rejected.",
        reason="Retry after review.",
    )


def test_review_batch_recomputes_exact_engine_resolution_and_blocked_plan_cancellation(catalog_world):
    draft, state = catalog_world
    actor, goal = _active_npc_with_goal(state)
    old_blocked = _blocked_plan(actor.id, goal.id, state.current_tick)
    state = state.model_copy(update={"npc_plans": [old_blocked]})
    state = _with_review_schedule(state, draft, actor.id)
    before_hash = state_hash(state)
    context = _context(draft, actor_id=state.player_id, state_version=31)
    plan = _simulation_plan(state.player_id or actor.id, state.current_tick)
    events = _review_events(state, plan, context)

    validation = validate_npc_plan_simulation_batch(state, events, plan, context)

    assert validation.mode == "review"
    assert validation.actual_npc_actor_id == actor.id
    assert validation.core_rule_events == ()
    assert validation.transition_outcome == "ready"
    assert [event.type for event in events] == [
        "core.npc_plan_revised.v1",
        "core.npc_plan_created.v1",
        "core.npc_plan_review_resolved.v1",
    ]
    assert events[0].plan.plan_id == old_blocked.plan_id
    assert events[0].plan.status == "cancelled"
    assert state_hash(state) == before_hash

    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, events[1:], plan, context)


def test_review_batch_rejects_missing_schedule_earlier_queue_and_stale_snapshot(catalog_world):
    draft, base = catalog_world
    actor, _ = _active_npc_with_goal(base)
    state = _with_review_schedule(base, draft, actor.id)
    context = _context(draft, state_version=14)
    plan = _simulation_plan(state.player_id or actor.id, state.current_tick)
    events = _review_events(state, plan, context)

    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(base, events, plan, context)

    earlier = ScheduledEventRecord(
        id=uuid4(), due_tick=state.current_tick, phase="decision", priority=100,
        handler_id="test.pending_decision", handler_version="1.0.0",
        payload={"title": "Earlier decision", "decision_options": []},
    )
    queued_state = state.model_copy(update={"scheduled_events": [*state.scheduled_events, earlier]})
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(queued_state, events, plan, context)

    review_index = next(index for index, event in enumerate(events) if isinstance(event, NpcPlanReviewResolvedEvent))
    stale_review = events[review_index].model_copy(update={"based_on_state_version": context.state_version + 1})
    stale = (*events[:review_index], stale_review)
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, stale, plan, context)


def test_review_batch_rejects_actor_payload_plan_and_reason_changes(catalog_world):
    draft, state = catalog_world
    actor, _ = _active_npc_with_goal(state)
    state = _with_review_schedule(state, draft, actor.id)
    context = _context(draft, state_version=25)
    plan = _simulation_plan(state.player_id or actor.id, state.current_tick)
    events = _review_events(state, plan, context)
    create_index = next(index for index, event in enumerate(events) if isinstance(event, NpcPlanCreatedEvent))
    review_index = next(index for index, event in enumerate(events) if isinstance(event, NpcPlanReviewResolvedEvent))
    review = events[review_index]
    created = events[create_index]
    supplied_response = DecisionResponse(
        request_id=review.decision_request_id,
        state_version=context.state_version,
        at_tick=review.at_tick,
        source=review.source,
        option_id="plan",
        explanation="The model used a different prose explanation.",
        npc_planning_status=review.outcome,
        npc_plan=created.plan,
        npc_plan_state_hash=state_hash(state),
        npc_model_call_id=review.model_call_id,
    )
    matching_context = context.model_copy(update={"decision_responses": (supplied_response,)})
    assert validate_npc_plan_simulation_batch(state, events, plan, matching_context).mode == "review"

    wrong_trace_response = supplied_response.model_copy(update={"npc_model_call_id": "another-call"})
    wrong_trace_context = context.model_copy(update={"decision_responses": (wrong_trace_response,)})
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, events, plan, wrong_trace_context)

    wrong_branch = list(events)
    wrong_branch[create_index] = wrong_branch[create_index].model_copy(update={"branch_id": uuid4()})
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, wrong_branch, plan, context)

    wrong_plan_id = list(events)
    original = wrong_plan_id[create_index]
    changed_plan = original.plan.model_copy(update={"plan_id": uuid4()})
    changed_create = original.model_copy(update={
        "event_id": npc_plan_event_id(
            context.command_id, context.ruleset_version, "created", changed_plan.plan_id, 1,
        ),
        "plan": changed_plan,
    })
    wrong_plan_id[create_index] = changed_create
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, wrong_plan_id, plan, context)

    wrong_reason = list(events)
    wrong_reason[review_index] = wrong_reason[review_index].model_copy(update={"reason": "changed_reason"})
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, wrong_reason, plan, context)

    wrong_actor = list(events)
    wrong_actor[review_index] = wrong_actor[review_index].model_copy(update={"actor_id": uuid4()})
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, wrong_actor, plan, context)

    stale_revision = list(events)
    review = stale_revision[review_index]
    expected_revision = review.expected_schedule_revision + 1
    schedule = next(item for item in state.scheduled_events if item.id == review.scheduled_event_id)
    revised_schedule = schedule.model_copy(update={"revision": expected_revision})
    stale_revision[review_index] = review.model_copy(update={
        "event_id": npc_plan_review_event_id(
            context.command_id, context.ruleset_version, schedule.id, expected_revision,
        ),
        "expected_schedule_revision": expected_revision,
        "decision_request_id": npc_plan_review_request_id(
            context.command_id, context.ruleset_version, revised_schedule,
        ),
    })
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, stale_revision, plan, context)


def test_policy_review_is_recomputed_without_fabricating_a_model_response(catalog_world):
    draft, base = catalog_world
    actor, _ = _active_npc_with_goal(base)
    state = _with_review_schedule(base, draft, actor.id)
    context = _context(draft, actor_id=state.player_id, externally_controlled={actor.id})
    plan = _simulation_plan(state.player_id or actor.id, state.current_tick)
    expected = SimulationEngine().plan_window(state, plan, context.model_copy(update={"decision_responses": ()}))
    assert expected.metrics.integration_boundary == "npc_plan_review_resolved"
    assert expected.events[-1].source == "policy"
    assert expected.events[-1].outcome == "skipped"

    validation = validate_npc_plan_simulation_batch(state, expected.events, plan, context)

    assert validation.mode == "review"
    assert validation.transition_outcome == "skipped"
    assert validation.actual_npc_actor_id == actor.id

    model_events = _review_events(state, plan, _context(draft, actor_id=state.player_id))
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, model_events, plan, context)


def test_policy_planner_response_without_call_id_is_replayed_truthfully(catalog_world):
    draft, base = catalog_world
    actor, _ = _active_npc_with_goal(base)
    state = _with_review_schedule(base, draft, actor.id)
    context = _context(draft, state_version=51)
    plan = _simulation_plan(state.player_id or actor.id, state.current_tick)
    events = _review_events(state, plan, context, source="policy", outcome="defer")

    validation = validate_npc_plan_simulation_batch(state, events, plan, context)

    assert validation.mode == "review"
    assert validation.transition_outcome == "defer"
    assert events[-1].source == "policy"
    assert events[-1].model_call_id is None


def _create_execution_plan(state, draft, actor_id: UUID, goal_id: UUID, step: NpcPlanStep):
    command_id = uuid4()
    plan = NpcPlan(
        plan_id=uuid4(),
        actor_id=actor_id,
        goal_ids=[goal_id],
        revision=1,
        status="ready",
        created_tick=state.current_tick,
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick,
        steps=[step],
        current_step=0,
        reason="Execute one bounded capability against current rules.",
    )
    event = NpcPlanCreatedEvent(
        event_id=npc_plan_event_id(command_id, draft.ruleset_version, "created", plan.plan_id, 1),
        world_id=draft.world_id,
        branch_id=draft.branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=draft.ruleset_version,
        plan=plan,
    )
    return apply_event(state, event), plan.plan_id


def _adult_pair_with_goal(state: WorldState):
    people = [
        entity for entity in state.entities.values()
        if entity.kind in {"character", "person"} and entity.status == "active" and entity.id != state.player_id
    ]
    for actor in people:
        goal = next((item for item in state.goals if item.owner_id == actor.id and item.status == "active"), None)
        if goal is None or actor.location_id is None:
            continue
        if actor.components.get("life", {}).get("life_stage") not in {"adult", "elder"}:
            continue
        for target in people:
            if target.id == actor.id or target.location_id != actor.location_id:
                continue
            if target.components.get("life", {}).get("life_stage") in {"adult", "elder"}:
                return actor, goal, target
    raise AssertionError("catalog world requires co-located adult NPCs and an owned active goal")


def test_execution_batch_returns_real_invitation_event_and_existing_validator_payload(catalog_world):
    draft, initial = catalog_world
    actor, goal, invitee = _adult_pair_with_goal(initial)
    state, plan_id = _create_execution_plan(
        initial,
        draft,
        actor.id,
        goal.id,
        NpcPlanStep(
            capability_id=capability_id_for_action("propose_relationship_invitation"),
            arguments={"target_id": str(invitee.id)},
        ),
    )
    context = _context(draft, actor_id=actor.id, state_version=61)
    plan = _simulation_plan(actor.id, state.current_tick)
    transition = build_npc_plan_execution_transition(state, plan_id, context)
    assert transition.outcome == "step_completed"
    before_hash = state_hash(state)

    validation = validate_npc_plan_simulation_batch(state, transition.events, plan, context)

    assert validation.mode == "execution"
    assert validation.actual_npc_actor_id == actor.id
    assert len(validation.core_rule_events) == 1
    assert validation.core_rule_events[0].type == "core.relationship_invitation_changed.v1"
    assert validation.action_command_type == "action.propose_relationship_invitation"
    assert validation.action_command_payload == {
        "kind": "propose_relationship_invitation",
        "target_id": str(invitee.id),
    }
    validate_relationship_invitation_batch(
        validation.core_rule_events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        actor_id=validation.actual_npc_actor_id,
        ruleset_version=context.ruleset_version,
        command_type=validation.action_command_type,
        command_payload=validation.action_command_payload,
    )
    assert state_hash(state) == before_hash
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, transition.events[:1] + transition.events[2:], plan, context)


def test_execution_batch_returns_real_ballot_vote_and_existing_validator_payload(catalog_world):
    draft, initial = catalog_world
    people = [
        entity for entity in initial.entities.values()
        if entity.kind in {"character", "person"} and entity.status == "active" and entity.id != initial.player_id
    ]
    incumbent, nominee = people[:2]
    voter = next(
        entity for entity in people[2:]
        if any(goal.owner_id == entity.id and goal.status == "active" for goal in initial.goals)
    )
    organization_id = uuid4()
    governance = GovernanceComponent(
        revision=1,
        members=sorted((incumbent.id, nominee.id, voter.id), key=str),
        leader_id=incumbent.id,
        succession_policy="election",
        decision_threshold=Decimal("0.5"),
        offices=[OfficeRecord(office_id="office.leader", title="Leader", holder_id=incumbent.id)],
    )
    organization = WorldEntity(
        id=organization_id,
        kind="organization",
        name="Review council",
        components={"governance": governance.model_dump(mode="json")},
    )
    raw = initial.model_dump(mode="json")
    raw["entities"][str(organization_id)] = organization.model_dump(mode="json")
    state = WorldState.model_validate(raw)
    ballot = open_leadership_ballot(
        state,
        ballot_id=uuid4(),
        organization_id=organization_id,
        nominee_id=nominee.id,
        actor_id=incumbent.id,
        existing_ballots=(),
    )
    raw = state.model_dump(mode="json")
    raw["leadership_ballots"] = [ballot.model_dump(mode="json")]
    state = WorldState.model_validate(raw)
    goal = next(goal for goal in state.goals if goal.owner_id == voter.id and goal.status == "active")
    state, plan_id = _create_execution_plan(
        state,
        draft,
        voter.id,
        goal.id,
        NpcPlanStep(
            capability_id=capability_id_for_action("cast_leadership_ballot_vote"),
            arguments={
                "ballot_id": str(ballot.ballot_id),
                "expected_ballot_revision": 1,
                "choice": "yes",
            },
        ),
    )
    context = _context(draft, actor_id=voter.id, state_version=62)
    plan = _simulation_plan(voter.id, state.current_tick)
    transition = build_npc_plan_execution_transition(state, plan_id, context)
    assert transition.outcome == "step_completed"

    validation = validate_npc_plan_simulation_batch(state, transition.events, plan, context)

    assert validation.actual_npc_actor_id == voter.id
    assert validation.action_command_type == "action.cast_leadership_ballot_vote"
    assert validation.action_command_payload == {
        "kind": "cast_leadership_ballot_vote",
        "ballot_id": str(ballot.ballot_id),
        "expected_ballot_revision": 1,
        "choice": "yes",
    }
    validate_leadership_ballot_batch(
        validation.core_rule_events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        actor_id=validation.actual_npc_actor_id,
        ruleset_version=context.ruleset_version,
        command_type=validation.action_command_type,
        command_payload=validation.action_command_payload,
    )


def test_execution_validator_does_not_accept_uncommitted_time_or_fake_events(catalog_world):
    draft, state = catalog_world
    actor, goal, _ = _adult_pair_with_goal(state)
    state, plan_id = _create_execution_plan(
        state,
        draft,
        actor.id,
        goal.id,
        NpcPlanStep(
            capability_id=capability_id_for_action("train"),
            arguments={"skill_id": "skill.local_knowledge", "duration_ticks": 600},
        ),
    )
    context = _context(draft, actor_id=actor.id, state_version=70)
    plan = _simulation_plan(actor.id, state.current_tick)
    transition = build_npc_plan_execution_transition(state, plan_id, context)
    assert transition.outcome == "interval_required"
    assert transition.events == ()
    assert transition.rule_resolution is not None
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(state, (), plan, context)

    engine_resolution = SimulationEngine().plan_window(state, plan, context)
    assert engine_resolution.metrics.integration_boundary == "npc_plan_execution_resolved"
    validation = validate_npc_plan_simulation_batch(state, engine_resolution.events, plan, context)
    assert validation.mode == "execution"
    assert validation.transition_outcome == "interval_required"
    assert validation.core_rule_events == ()


def test_execution_validator_rechecks_actual_journey_receipt(catalog_world):
    draft, initial = catalog_world
    actor = goal = route = None
    for candidate, candidate_goal in (
        (entity, item)
        for item in initial.goals
        for entity in [initial.entities.get(item.owner_id)]
        if entity is not None
        and entity.kind in {"character", "person"}
        and entity.status == "active"
        and entity.id != initial.player_id
        and item.status == "active"
    ):
        if candidate.location_id is None:
            continue
        origin = initial.entities.get(candidate.location_id)
        if origin is None:
            continue
        place = PlaceComponent.model_validate(origin.components.get("place"))
        chosen = next((
            item for item in place.routes
            if item.is_open
            and (destination := initial.entities.get(item.to_place_id)) is not None
            and destination.status == "active"
        ), None)
        if chosen is not None:
            actor, goal, route = candidate, candidate_goal, chosen
            break
    assert actor is not None and goal is not None and route is not None

    # This test needs an arrival receipt. Hazard interruption receipts have
    # separate execution coverage, so make this particular fixture route safe.
    origin = initial.entities[actor.location_id]
    place = PlaceComponent.model_validate(origin.components["place"])
    safe_route = route.model_copy(update={"risk": Decimal("0")})
    safe_place = place.model_copy(update={
        "routes": [safe_route if row.route_id == route.route_id else row for row in place.routes],
    })
    raw = initial.model_dump(mode="json")
    raw["entities"][str(origin.id)]["components"]["place"] = safe_place.model_dump(mode="json")
    initial = WorldState.model_validate(raw)

    state, plan_id = _create_execution_plan(
        initial,
        draft,
        actor.id,
        goal.id,
        NpcPlanStep(
            capability_id=capability_id_for_action("move"),
            arguments={"destination_id": str(route.to_place_id), "route_id": str(route.route_id)},
        ),
    )
    start_context = _context(draft, actor_id=actor.id, state_version=81)
    simulation_plan = _simulation_plan(actor.id, state.current_tick)
    start = build_npc_plan_execution_transition(state, plan_id, start_context)
    assert start.outcome == "journey_started"

    start_validation = validate_npc_plan_simulation_batch(state, start.events, simulation_plan, start_context)
    departure = next(event for event in start.rule_resolution.events if isinstance(event, TravelJourneyChangedEvent))
    assert start_validation.core_rule_events == (departure,)
    in_transit = state
    for event in start.events:
        in_transit = apply_event(in_transit, event)
    journey = next(item for item in in_transit.travel_journeys if item.actor_id == actor.id)
    assert journey.hazard_tick is None

    advance_context = _context(draft, actor_id=actor.id, state_version=82)
    advance = SimulationEngine().plan_window(
        in_transit,
        WorldAdvancePlan(started_tick=in_transit.current_tick, target_tick=journey.arrival_tick),
        advance_context,
    )
    at_due = in_transit
    for event in advance.events:
        at_due = apply_event(at_due, event)
    assert at_due.current_tick == journey.arrival_tick

    resolver_context = _context(draft, actor_id=actor.id, state_version=83)
    resolution = SimulationEngine().plan_window(
        at_due,
        WorldAdvancePlan(started_tick=at_due.current_tick, target_tick=at_due.current_tick),
        resolver_context,
    )
    receipt = next(
        event for event in resolution.events
        if isinstance(event, TravelJourneyChangedEvent) and event.operation == "arrive"
    )
    arrived = at_due
    for event in resolution.events:
        arrived = apply_event(arrived, event)

    complete_context = _context(draft, actor_id=actor.id, state_version=84)
    complete = build_npc_plan_execution_transition(
        arrived,
        plan_id,
        complete_context,
        journey_resolution_receipts={journey.journey_id: receipt},
    )
    assert complete.outcome == "journey_completed"
    complete_validation = validate_npc_plan_simulation_batch(
        arrived,
        complete.events,
        _simulation_plan(actor.id, arrived.current_tick),
        complete_context,
        journey_resolution_receipts={journey.journey_id: receipt},
    )
    assert complete_validation.core_rule_events == ()

    fake_receipt = receipt.model_copy(update={"branch_id": uuid4()})
    with pytest.raises(InvalidEvent):
        validate_npc_plan_simulation_batch(
            arrived,
            complete.events,
            _simulation_plan(actor.id, arrived.current_tick),
            complete_context,
            journey_resolution_receipts={journey.journey_id: fake_receipt},
        )

