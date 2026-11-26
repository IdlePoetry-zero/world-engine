from uuid import uuid4, uuid5

import pytest

from world_engine.domain.ambient_events import AmbientEventPolicy, AmbientEventProposal, build_ambient_event_schedule
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.components import RelationRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.domain.social_contact_settlement import build_social_contact_schedules
from world_engine.simulation.contracts import DecisionResponse, SimulationContext, WorldAdvancePlan
from world_engine.simulation.engine import SimulationEngine


def test_ambient_model_proposal_uses_the_authoritative_simulation_boundary():
    world, branch, place, witness = [uuid4() for _ in range(4)]
    state = WorldState(entities={
        place: WorldEntity(id=place, kind="place", name="Landing"),
        witness: WorldEntity(id=witness, kind="character", name="Courier", location_id=place),
    })
    schedule = build_ambient_event_schedule(
        state, AmbientEventPolicy(scope_kind="region", scope_id=place, check_every_days=1,
                                  chance_basis_points=10_000), first_tick=0, max_runs=2,
    )
    state = state.model_copy(update={"scheduled_events": [schedule]})
    plan = WorldAdvancePlan(started_tick=0, target_tick=60)
    context = SimulationContext(world_id=world, branch_id=branch, command_id=uuid4(),
                                world_seed=str(world), state_version=0, allow_npc_routine_mobility=True)
    engine = SimulationEngine()
    checkpoint = engine.plan_window(state, plan, context)
    assert checkpoint.outcome == "decision_required"
    assert not checkpoint.events
    request, = checkpoint.decision_requests
    assert request.causal_subject_id == schedule.id
    response = DecisionResponse(
        request_id=request.request_id, state_version=0, at_tick=0, option_id="ambient",
        ambient_outcome="accepted", ambient_state_hash=state_hash(state),
        ambient_model_call_id="test-realistic-call-reference",
        ambient_proposal=AmbientEventProposal(kind="local_observation", place_id=place, initiator_id=witness,
                                              title="Delay at the crossing", description="The ferry is late."),
    )
    resolution = engine.plan_window(state, plan, context.model_copy(update={"decision_responses": (response,)}))
    assert resolution.outcome == "advanced" and resolution.next_tick == 0
    event, = resolution.events
    assert event.outcome == "accepted"
    after = apply_event(state, event)
    assert after.ambient_events[0].proposal == response.ambient_proposal
    assert after.knowledge[0].holder_id == witness
    assert state_hash(state) == response.ambient_state_hash


def test_daily_social_contact_checks_a_prerequisite_and_replays_private_score_effects():
    world, place, first, second, managed, remote = [uuid4() for _ in range(6)]
    state = WorldState(metadata={"social_world_v1": True}, entities={
        place: WorldEntity(id=place, kind="place", name="A market"),
        remote: WorldEntity(id=remote, kind="place", name="Another market"),
        first: WorldEntity(id=first, kind="person", name="First person", location_id=place),
        second: WorldEntity(id=second, kind="person", name="Second person", location_id=place),
        managed: WorldEntity(id=managed, kind="person", name="Controlled person", location_id=place),
    }, relations=[
        RelationRecord(id=uuid4(), from_id=first, to_id=second, dimensions={"familiarity": 12}),
        RelationRecord(id=uuid4(), from_id=first, to_id=managed),
    ])
    schedule = build_social_contact_schedules(state, world)[0]
    tick = schedule.due_tick
    at_slot = state.model_copy(update={"current_tick": tick, "scheduled_events": [schedule]})
    engine = SimulationEngine()
    plan = WorldAdvancePlan(started_tick=0, target_tick=tick + 20)
    for alternative in range(40):
        context = SimulationContext(world_id=world, branch_id=uuid5(world, str(alternative)), command_id=uuid4(),
                                    world_seed=str(world), state_version=0, allow_npc_routine_mobility=True,
                                    externally_controlled_actor_ids=frozenset({managed}))
        resolution = engine.plan_window(at_slot, plan, context)
        event, = resolution.events
        choice, = event.choices
        if choice.outcome == "exchanged_words":
            break
    else:
        pytest.fail("none of the deterministic draws selected a routine exchange")
    assert {first, second} == set(choice.opportunity.participant_ids)
    assert len(event.relationship_effects) == 2
    assert event.visibility == {"policy": "author_only"}
    after = apply_event(at_slot, event)
    restored = parse_event(event.model_dump(mode="json"))
    assert state_hash(apply_event(at_slot, restored)) == state_hash(after)
    assert after.scheduled_events[0].due_tick == tick + state.calendar.day_seconds
    assert [row.routine_social_contacts for row in after.relations] == [1, 0, 1]
    with pytest.raises(InvalidEvent, match="does not match"):
        apply_event(at_slot, event.model_copy(update={"relationship_effects": ()}))
