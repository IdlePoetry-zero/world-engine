from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.content import ContentCatalog
from world_engine.domain.components import (
    GoalCondition,
    GoalRecord,
    GovernanceComponent,
    MarketComponent,
    OfficeRecord,
    PlaceComponent,
    PlaceRoute,
)
from world_engine.domain.leadership_ballots import open_leadership_ballot
from world_engine.domain.models import (
    EntityFieldChangedEvent,
    MarketTradeExecutedEvent,
    NpcPlanCreatedEvent,
    ResourceAccount,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.npc_plan_events import npc_plan_event_id
from world_engine.domain.npc_goal_completion import NPC_GOAL_COMPLETION_POLICY
from world_engine.domain.npc_plans import ActiveNpcExecution, NpcPlan, NpcPlanStep
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.simulation.contracts import SimulationContext, WorldAdvancePlan
from world_engine.simulation.engine import SimulationEngine
from world_engine.simulation.npc_plan_execution import build_npc_plan_execution_transition
from world_engine.simulation.npc_capabilities import capability_id_for_action


@pytest.fixture(scope="module")
def catalog_world():
    draft = ContentCatalog().build_world("modern_city", "NPC plan execution tests", "Test Player", seed=91)
    return draft, draft.state


def _context(
    draft,
    *,
    command_id: UUID | None = None,
    state_version: int = 1,
    externally_controlled_actor_ids: frozenset[UUID] = frozenset(),
    branch_id: UUID | None = None,
    journey_receipt_branch_ids: frozenset[UUID] = frozenset(),
    command_actor_id: UUID | None = None,
) -> SimulationContext:
    return SimulationContext(
        world_id=draft.world_id,
        branch_id=branch_id or draft.branch_id,
        command_id=command_id or uuid4(),
        command_actor_id=command_actor_id,
        world_seed="npc-plan-execution-test",
        state_version=state_version,
        ruleset_version=draft.ruleset_version,
        externally_controlled_actor_ids=externally_controlled_actor_ids,
        journey_receipt_branch_ids=journey_receipt_branch_ids,
    )


def _goal(state: WorldState, actor_id: UUID) -> GoalRecord:
    return next(
        item for item in state.goals
        if item.owner_id == actor_id and item.status == "active"
    )


def _step(action: str, arguments: dict[str, object]) -> NpcPlanStep:
    return NpcPlanStep(
        capability_id=capability_id_for_action(action),
        arguments=arguments,
    )


def _create_plan(
    state: WorldState,
    draft,
    actor_id: UUID,
    step: NpcPlanStep,
    *,
    due_tick: int | None = None,
    goal_id: UUID | None = None,
) -> tuple[WorldState, UUID]:
    plan_id = uuid4()
    create_command_id = uuid4()
    goal_id = goal_id or _goal(state, actor_id).id
    plan = NpcPlan(
        plan_id=plan_id,
        actor_id=actor_id,
        goal_ids=[goal_id],
        revision=1,
        status="ready",
        created_tick=state.current_tick,
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick if due_tick is None else due_tick,
        steps=[step],
        current_step=0,
        attempts=0,
        reason="Pursue the actor's active goal through a registered rule action.",
    )
    event = NpcPlanCreatedEvent(
        event_id=npc_plan_event_id(create_command_id, draft.ruleset_version, "created", plan_id, 1),
        world_id=draft.world_id,
        branch_id=draft.branch_id,
        command_id=create_command_id,
        actor_id=actor_id,
        ruleset_version=draft.ruleset_version,
        plan=plan,
    )
    return apply_event(state, event), plan_id


def _active_people(state: WorldState) -> list[WorldEntity]:
    return [
        entity for entity in state.entities.values()
        if entity.kind in {"character", "person"}
        and entity.status == "active"
        and entity.id != state.player_id
    ]


def _actor_with_route(
    state: WorldState,
    *,
    exclude: set[UUID] | None = None,
    safe_only: bool = False,
) -> tuple[WorldEntity, PlaceRoute]:
    excluded = exclude or set()
    for actor in _active_people(state):
        if actor.id in excluded or actor.location_id is None:
            continue
        origin = state.entities.get(actor.location_id)
        if origin is None:
            continue
        try:
            routes = PlaceComponent.model_validate(origin.components.get("place")).routes
        except Exception:
            continue
        for route in routes:
            destination = state.entities.get(route.to_place_id)
            if (
                route.is_open
                and (not safe_only or route.risk == Decimal("0"))
                and destination is not None
                and destination.status == "active"
            ):
                if any(item.actor_id == actor.id and item.status == "in_transit" for item in state.travel_journeys):
                    continue
                if any(goal.owner_id == actor.id and goal.status == "active" for goal in state.goals):
                    return actor, route
    raise AssertionError("the modern catalog fixture must have an active NPC with an open route and goal")


def _adult_pair_with_goal(state: WorldState) -> tuple[WorldEntity, WorldEntity]:
    people = _active_people(state)
    for actor in people:
        if not any(goal.owner_id == actor.id and goal.status == "active" for goal in state.goals):
            continue
        for target in people:
            if target.id == actor.id or target.location_id != actor.location_id or target.location_id is None:
                continue
            if (
                actor.components.get("life", {}).get("life_stage") in {"adult", "elder"}
                and target.components.get("life", {}).get("life_stage") in {"adult", "elder"}
            ):
                return actor, target
    raise AssertionError("the catalog fixture must include co-located adults and an active NPC goal")


def _apply_all(state: WorldState, events) -> WorldState:
    projected = state
    for event in events:
        projected = apply_event(projected, event)
    return projected


def test_instant_invitation_keeps_rule_causation_and_completes_from_real_event(catalog_world):
    draft, state = catalog_world
    proposer, invitee = _adult_pair_with_goal(state)
    state, plan_id = _create_plan(
        state, draft, proposer.id,
        _step("propose_relationship_invitation", {"target_id": str(invitee.id)}),
    )
    context = _context(draft)
    before_hash = state_hash(state)

    transition = build_npc_plan_execution_transition(state, plan_id, context)

    assert transition.outcome == "step_completed"
    assert transition.committed is False and transition.time_advanced is False
    assert transition.tick_before == transition.tick_after == state.current_tick
    assert transition.source_state_hash == transition.input_state_hash_after == before_hash
    assert transition.projected_state_hash == state_hash(_apply_all(state, transition.events))
    assert transition.source_revision == 1 and transition.result_revision == 3
    assert transition.source_current_step == 0 and transition.result_current_step == 1
    assert transition.consumed_step_index == 0
    assert [event.type for event in transition.events] == [
        "core.npc_plan_revised.v1",
        "core.relationship_invitation_changed.v1",
        "core.npc_plan_revised.v1",
    ]
    assert transition.rule_resolution is not None
    assert transition.rule_resolution.outcome.status == "succeeded"
    assert tuple(event.event_id for event in transition.rule_resolution.events) == transition.evidence_event_ids

    projected = _apply_all(state, transition.events)
    invitation = next(item for item in projected.relationship_invitations if item.proposer_id == proposer.id)
    completed = next(item for item in projected.npc_plans if item.plan_id == plan_id)
    assert invitation.invitee_id == invitee.id
    assert invitation.status == "pending"
    assert completed.status == "completed"
    assert completed.current_step == len(completed.steps) == 1
    assert completed.evidence_refs == list(transition.evidence_event_ids)
    assert state_hash(state) == before_hash


def test_instant_ballot_vote_changes_the_actual_ballot_and_not_only_the_plan(catalog_world):
    draft, state = catalog_world
    people = _active_people(state)
    incumbent, nominee, voter = people[:3]
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
        name="Execution Fixture Council",
        components={"governance": governance.model_dump(mode="json")},
    )
    raw = state.model_dump(mode="json")
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
    if not any(goal.owner_id == voter.id and goal.status == "active" for goal in state.goals):
        goal = GoalRecord(
            id=uuid4(), owner_id=voter.id, title="Cast a council vote", status="active",
            created_tick=state.current_tick, updated_tick=state.current_tick,
        )
        raw = state.model_dump(mode="json")
        raw["goals"] = [*raw["goals"], goal.model_dump(mode="json")]
        state = WorldState.model_validate(raw)
    state, plan_id = _create_plan(
        state, draft, voter.id,
        _step("cast_leadership_ballot_vote", {
            "ballot_id": str(ballot.ballot_id),
            "expected_ballot_revision": 1,
            "choice": "yes",
        }),
    )

    transition = build_npc_plan_execution_transition(state, plan_id, _context(draft))

    assert transition.outcome == "step_completed"
    assert transition.rule_resolution is not None
    vote_event = next(
        event for event in transition.rule_resolution.events
        if event.type == "core.leadership_ballot_changed.v1"
    )
    projected = _apply_all(state, transition.events)
    updated_ballot = next(item for item in projected.leadership_ballots if item.ballot_id == ballot.ballot_id)
    assert [(vote.voter_id, vote.choice) for vote in updated_ballot.votes] == [(voter.id, "yes")]
    assert vote_event.event_id in projected.npc_plans[0].evidence_refs
    assert projected.npc_plans[0].status == "completed"
    assert projected.current_tick == state.current_tick


def test_two_npcs_can_start_real_journeys_at_one_tick_with_distinct_commands(catalog_world):
    draft, state = catalog_world
    first, first_route = _actor_with_route(state)
    second, second_route = _actor_with_route(state, exclude={first.id})
    state, first_plan_id = _create_plan(
        state, draft, first.id,
        _step("move", {"destination_id": str(first_route.to_place_id)}),
    )
    state, second_plan_id = _create_plan(
        state, draft, second.id,
        _step("move", {"destination_id": str(second_route.to_place_id)}),
    )
    first_context = _context(draft, state_version=10)
    second_context = _context(draft, state_version=11)
    assert first_context.command_id != second_context.command_id
    start_tick = state.current_tick

    first_transition = build_npc_plan_execution_transition(state, first_plan_id, first_context)
    assert first_transition.outcome == "journey_started"
    projected = _apply_all(state, first_transition.events)
    second_transition = build_npc_plan_execution_transition(projected, second_plan_id, second_context)
    assert second_transition.outcome == "journey_started"
    projected = _apply_all(projected, second_transition.events)

    journeys = [journey for journey in projected.travel_journeys if journey.actor_id in {first.id, second.id}]
    assert len(journeys) == 2
    assert {journey.departed_tick for journey in journeys} == {start_tick}
    assert len({journey.journey_id for journey in journeys}) == 2
    assert projected.current_tick == start_tick
    assert first_transition.time_advanced is second_transition.time_advanced is False
    for plan_id in (first_plan_id, second_plan_id):
        running = next(item for item in projected.npc_plans if item.plan_id == plan_id)
        assert running.status == "running"
        assert running.active_execution is not None
        assert running.active_execution.start_event_id in running.evidence_refs
        assert running.next_review_tick == running.active_execution.due_tick


def _start_catalog_journey(draft, source_state, *, route_risk: Decimal = Decimal("0"), goal_completion=False):
    actor, route = _actor_with_route(source_state)
    origin = source_state.entities[actor.location_id]
    place = PlaceComponent.model_validate(origin.components["place"])
    route = route.model_copy(update={"risk": route_risk})
    safe_place = place.model_copy(update={
        "routes": [route if item.route_id == route.route_id else item for item in place.routes],
    })
    raw = source_state.model_dump(mode="json")
    raw["entities"][str(origin.id)]["components"]["place"] = safe_place.model_dump(mode="json")
    if goal_completion:
        raw["metadata"][NPC_GOAL_COMPLETION_POLICY] = True
        target_goal = next(goal for goal in raw["goals"] if goal["id"] == str(_goal(source_state, actor.id).id))
        target_goal["conditions"] = [GoalCondition(
            kind="reach_location", subject_id=actor.id, target_id=route.to_place_id,
        ).model_dump(mode="json")]
    source_state = WorldState.model_validate(raw)
    state, plan_id = _create_plan(
        source_state, draft, actor.id,
        _step("move", {
            "destination_id": str(route.to_place_id),
            "route_id": str(route.route_id),
        }),
    )
    start_context = _context(draft, state_version=20)
    transition = build_npc_plan_execution_transition(state, plan_id, start_context)
    assert transition.outcome == "journey_started"
    state = _apply_all(state, transition.events)
    journey = next(item for item in state.travel_journeys if item.actor_id == actor.id)
    return state, plan_id, journey


def _resolve_with_simulation_engine(draft, state, journey, *, branch_id: UUID | None = None):
    expected_operation = "interrupt" if journey.hazard_tick is not None else "arrive"
    due_tick = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
    engine = SimulationEngine()
    advance = engine.plan_window(
        state,
        WorldAdvancePlan(started_tick=state.current_tick, target_tick=due_tick),
        _context(draft, state_version=21, branch_id=branch_id, command_actor_id=uuid4()),
    )
    at_due = _apply_all(state, advance.events)
    assert at_due.current_tick == due_tick
    resolution = engine.plan_window(
        at_due,
        WorldAdvancePlan(started_tick=at_due.current_tick, target_tick=at_due.current_tick),
        _context(draft, state_version=22, branch_id=branch_id, command_actor_id=uuid4()),
    )
    arrival = next(
        event for event in resolution.events
        if isinstance(event, TravelJourneyChangedEvent) and event.operation == expected_operation
    )
    assert any(isinstance(event, TimeAdvancedEvent) for event in advance.events)
    projected = _apply_all(at_due, resolution.events)
    assert projected.current_tick == due_tick
    return projected, arrival


@pytest.mark.parametrize("goal_completion", [False, True])
def test_journey_completes_only_after_simulation_engine_arrival_and_matching_receipt(catalog_world, goal_completion):
    draft, source_state = catalog_world
    state, plan_id, journey = _start_catalog_journey(draft, source_state, goal_completion=goal_completion)
    assert state.current_tick == journey.departed_tick
    arrived_state, receipt = _resolve_with_simulation_engine(draft, state, journey)
    assert arrived_state.current_tick == journey.arrival_tick

    missing = build_npc_plan_execution_transition(
        arrived_state, plan_id, _context(draft, state_version=22),
    )
    assert missing.outcome == "waiting_for_journey"
    assert missing.reason_code == "npc_journey_resolution_receipt_missing"
    assert missing.events == ()
    assert next(item for item in arrived_state.npc_plans if item.plan_id == plan_id).status == "running"

    complete = build_npc_plan_execution_transition(
        arrived_state,
        plan_id,
        _context(draft, state_version=22),
        journey_resolution_receipts={journey.journey_id: receipt},
    )
    assert complete.outcome == "journey_completed"
    assert complete.evidence_event_ids == (receipt.event_id,)
    assert len(complete.events) == 1
    completed_state = _apply_all(arrived_state, complete.events)
    completed = next(item for item in completed_state.npc_plans if item.plan_id == plan_id)
    assert completed.status == "completed"
    assert completed.current_step == 1
    assert receipt.event_id in completed.evidence_refs
    assert completed_state.current_tick == arrived_state.current_tick
    goal_id, = completed.goal_ids
    goal = next(item for item in completed_state.goals if item.id == goal_id)
    assert complete.events[0].fulfilled_goal_ids == ((goal_id,) if goal_completion else ())
    assert goal.status == ("succeeded" if goal_completion else "active")
    if goal_completion:
        assert goal.evidence_refs == [receipt.event_id]


def test_wrong_world_or_branch_receipt_is_rejected_without_success(catalog_world):
    draft, source_state = catalog_world
    state, plan_id, journey = _start_catalog_journey(draft, source_state)
    arrived_state, receipt = _resolve_with_simulation_engine(draft, state, journey)
    wrong_scope = TravelJourneyChangedEvent.model_validate({
        **receipt.model_dump(mode="python"),
        "branch_id": uuid4(),
    })

    transition = build_npc_plan_execution_transition(
        arrived_state,
        plan_id,
        _context(draft, state_version=22),
        journey_resolution_receipts={journey.journey_id: wrong_scope},
    )

    assert transition.outcome == "invalid_receipt"
    assert transition.reason_code == "npc_journey_resolution_receipt_mismatch"
    assert transition.events == ()
    still_running = next(item for item in arrived_state.npc_plans if item.plan_id == plan_id)
    assert still_running.status == "running"
    assert still_running.current_step == 0


def test_interrupted_journey_keeps_failure_and_blocks_the_unconsumed_step(catalog_world):
    draft, source_state = catalog_world
    state, plan_id, journey = _start_catalog_journey(
        draft, source_state, route_risk=Decimal("1"),
    )
    assert journey.hazard_tick is not None
    interrupted_state, receipt = _resolve_with_simulation_engine(draft, state, journey)
    assert receipt.operation == "interrupt"

    transition = build_npc_plan_execution_transition(
        interrupted_state,
        plan_id,
        _context(draft, state_version=23),
        journey_resolution_receipts={journey.journey_id: receipt},
    )

    assert transition.outcome == "journey_interrupted"
    assert transition.next_due_tick is not None and transition.next_due_tick > interrupted_state.current_tick
    blocked_state = _apply_all(interrupted_state, transition.events)
    blocked = next(item for item in blocked_state.npc_plans if item.plan_id == plan_id)
    assert blocked.status == "blocked"
    assert blocked.current_step == 0
    assert blocked.active_execution is None
    assert "travel_interrupted" in blocked.last_failure
    assert receipt.event_id in blocked.evidence_refs


def test_intermediate_ancestor_receipt_requires_host_verified_branch_lineage(catalog_world):
    draft, source_state = catalog_world
    state, plan_id, journey = _start_catalog_journey(draft, source_state)
    branch_b, branch_c = uuid4(), uuid4()
    arrived_state, receipt = _resolve_with_simulation_engine(
        draft, state, journey, branch_id=branch_b,
    )

    unverified = build_npc_plan_execution_transition(
        arrived_state,
        plan_id,
        _context(draft, state_version=24, branch_id=branch_c),
        journey_resolution_receipts={journey.journey_id: receipt},
    )
    assert unverified.outcome == "invalid_receipt"

    verified = build_npc_plan_execution_transition(
        arrived_state,
        plan_id,
        _context(
            draft,
            state_version=24,
            branch_id=branch_c,
            journey_receipt_branch_ids=frozenset({branch_b}),
        ),
        journey_resolution_receipts={journey.journey_id: receipt},
    )
    assert verified.outcome == "journey_completed"
    assert verified.evidence_event_ids == (receipt.event_id,)
    running = next(item for item in arrived_state.npc_plans if item.plan_id == plan_id)
    assert running.active_execution is not None
    assert running.active_execution.branch_id == draft.branch_id


def test_death_terminates_an_in_flight_plan_without_acting_for_the_actor(catalog_world):
    draft, source_state = catalog_world
    state, plan_id, journey = _start_catalog_journey(draft, source_state)
    actor = state.entities[journey.actor_id]
    population_revision = None
    population_group_id = actor.components["life"].get("population_group_id")
    if population_group_id is not None:
        group = state.entities[UUID(str(population_group_id))]
        population_revision = group.components["population"]["revision"]
    dead_state = apply_event(state, EntityFieldChangedEvent(
        entity_id=actor.id,
        field_name="status",
        value="dead",
        expected_population_revision=population_revision,
    ))

    transition = build_npc_plan_execution_transition(
        dead_state, plan_id, _context(draft, state_version=22),
    )

    assert transition.outcome == "cancelled", transition.reason_code
    assert transition.reason_code == "npc_plan_actor_unavailable"
    assert len(transition.events) == 1
    cancelled_state = _apply_all(dead_state, transition.events)
    cancelled = next(item for item in cancelled_state.npc_plans if item.plan_id == plan_id)
    assert cancelled.status == "cancelled"
    assert cancelled.active_execution is None
    assert cancelled.current_step == 0
    assert cancelled_state.travel_journeys[0].status == "in_transit"


def _blocked_trade_state() -> tuple[WorldState, UUID, UUID]:
    actor_id, seller_id, place_id = uuid4(), uuid4(), uuid4()
    listing_id, stock_account_id, stock_lot_id, goal_id = uuid4(), uuid4(), uuid4(), uuid4()
    place = WorldEntity(
        id=place_id,
        kind="place",
        name="Market square",
        components={
            "place": PlaceComponent(place_type="settlement").model_dump(mode="json"),
            "market": MarketComponent(
                currency_resource_id="core.coin",
                offers=[{
                    "listing_id": listing_id,
                    "resource_id": "core.ore",
                    "stock_account_id": stock_account_id,
                    "seller_id": seller_id,
                    "unit_price_minor": 5,
                    "minimum_quantity": "1",
                    "active": True,
                }],
            ).model_dump(mode="json"),
        },
    )
    actor = WorldEntity(
        id=actor_id, kind="character", name="Buyer", location_id=place_id,
        components={"inventory": {"lots": []}},
    )
    seller = WorldEntity(
        id=seller_id, kind="character", name="Seller", location_id=place_id,
        components={"inventory": {"lots": [{
            "lot_id": stock_lot_id,
            "account_id": stock_account_id,
            "resource_id": "core.ore",
            "quantity": "2",
        }]}},
    )
    goal = GoalRecord(
        id=goal_id,
        owner_id=actor_id,
        title="Buy ore",
        status="active",
        created_tick=0,
        updated_tick=0,
    )
    state = WorldState(
        entities={actor_id: actor, seller_id: seller, place_id: place},
        resources={stock_account_id: ResourceAccount(
            resource_id="core.ore", balance="2", owner_id=seller_id,
        )},
        goals=[goal],
    )
    return state, actor_id, listing_id


def test_rejected_trade_blocks_with_positive_backoff_and_never_repeats_resource_events():
    draft = ContentCatalog().build_world("modern_city", "NPC blocked trade", "Player", seed=17)
    state, actor_id, listing_id = _blocked_trade_state()
    state, plan_id = _create_plan(
        state, draft, actor_id,
        _step("trade", {"listing_id": str(listing_id), "quantity": "1"}),
    )
    before_resources = state.resources.copy()

    first = build_npc_plan_execution_transition(state, plan_id, _context(draft, state_version=1))
    assert first.outcome == "blocked"
    assert first.rule_resolution is not None
    assert first.rule_resolution.outcome.status == "rejected"
    assert first.rule_resolution.events == []
    assert first.next_due_tick is not None and first.next_due_tick > state.current_tick
    blocked_state = _apply_all(state, first.events)
    blocked = next(item for item in blocked_state.npc_plans if item.plan_id == plan_id)
    assert blocked.status == "blocked"
    assert blocked.last_failure
    assert blocked_state.resources == before_resources
    assert not any(isinstance(event, MarketTradeExecutedEvent) for event in first.events)

    retry_early = build_npc_plan_execution_transition(
        blocked_state, plan_id, _context(draft, state_version=2),
    )
    assert retry_early.outcome == "not_due"
    assert retry_early.events == ()
    assert retry_early.next_due_tick == first.next_due_tick
    assert blocked_state.resources == before_resources


def test_interval_action_is_reported_without_committing_rule_events_or_time(catalog_world):
    draft, state = catalog_world
    actor = next(
        entity for entity in _active_people(state)
        if entity.components.get("skills")
        and any(goal.owner_id == entity.id and goal.status == "active" for goal in state.goals)
    )
    skill_id = next(iter(actor.components["skills"]["skills"]))
    state, plan_id = _create_plan(
        state, draft, actor.id,
        _step("train", {"skill_id": skill_id, "duration_ticks": 600}),
    )
    before = state_hash(state)

    transition = build_npc_plan_execution_transition(state, plan_id, _context(draft))

    assert transition.outcome == "interval_required"
    assert transition.required_ticks == 600
    assert transition.events == ()
    assert transition.rule_resolution is not None and transition.rule_resolution.events
    assert transition.tick_before == transition.tick_after == state.current_tick
    assert state_hash(state) == before


def test_active_execution_schema_is_optional_but_running_journey_shape_is_strict(catalog_world):
    draft, source_state = catalog_world
    actor, route = _actor_with_route(source_state)
    state, plan_id = _create_plan(
        source_state, draft, actor.id,
        _step("move", {"destination_id": str(route.to_place_id)}),
    )
    plan = next(item for item in state.npc_plans if item.plan_id == plan_id)
    assert "active_execution" not in plan.model_dump(mode="json")

    start = build_npc_plan_execution_transition(state, plan_id, _context(draft))
    started_state = _apply_all(state, start.events)
    running = next(item for item in started_state.npc_plans if item.plan_id == plan_id)
    assert running.active_execution is not None
    assert running.active_execution.kind == "journey"
    assert running.active_execution.start_event_id == start.evidence_event_ids[0]
    assert NpcPlan.model_validate(running.model_dump(mode="json")) == running
    with pytest.raises(ValidationError):
        NpcPlan.model_validate({
            **running.model_dump(mode="python"),
            "status": "ready",
        })
    with pytest.raises(ValidationError):
        ActiveNpcExecution(
            journey_id=uuid4(),
            start_event_id=uuid4(),
            start_command_id=uuid4(),
            world_id=draft.world_id,
            branch_id=draft.branch_id,
            ruleset_version=draft.ruleset_version,
            started_tick=4,
            due_tick=4,
        )
