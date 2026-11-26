from uuid import uuid4

import pytest

from world_engine.application.visibility import project_player_state
from world_engine.domain.ambient_events import (
    AmbientEventPolicy, AmbientEventProposal, AmbientEventResolvedEvent, ambient_event_id,
    ambient_agenda_author_ids, ambient_participants, ambient_roll, build_ambient_event_schedule,
    validate_ambient_proposal,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event


def test_calendar_opportunity_is_stable_on_retry_and_keeps_local_actor_scope():
    region, square, other_place, npc, player, remote = [uuid4() for _ in range(6)]
    state = WorldState(player_id=player, entities={
        region: WorldEntity(id=region, kind="place", name="Region"),
        square: WorldEntity(id=square, kind="place", name="Square", location_id=region),
        other_place: WorldEntity(id=other_place, kind="place", name="Elsewhere"),
        npc: WorldEntity(id=npc, kind="character", name="Courier", location_id=square),
        player: WorldEntity(id=player, kind="character", name="Traveler", location_id=square),
        remote: WorldEntity(id=remote, kind="character", name="Innkeeper", location_id=other_place),
    })
    policy = AmbientEventPolicy(scope_kind="region", scope_id=region, check_every_days=7, chance_basis_points=1800)
    schedule = build_ambient_event_schedule(state, policy, first_tick=60, max_runs=10)
    branch = uuid4()
    draw = ambient_roll("fixture", branch, schedule)
    assert 0 <= draw < 10_000
    assert ambient_roll("fixture", branch, schedule.model_copy()) == draw
    assert schedule.repeat_ticks == state.calendar.day_seconds * 7
    assert ambient_participants(state, policy) == {npc: square}
    assert ambient_participants(state, policy, controlled=frozenset({npc})) == {}

    proposal = AmbientEventProposal(kind="request_for_help", place_id=square, initiator_id=npc,
                                    title="A request for help", description="The courier seeks a spare hand.")
    validate_ambient_proposal(proposal, ambient_participants(state, policy))
    with pytest.raises(ValueError, match="eligible and present"):
        validate_ambient_proposal(proposal.model_copy(update={"participant_ids": (remote,)}),
                                  ambient_participants(state, policy))
    with pytest.raises(ValueError, match="declared kind"):
        build_ambient_event_schedule(state, policy.model_copy(update={"scope_kind": "organization"}),
                                     first_tick=60, max_runs=10)


def test_accepted_event_advances_its_schedule_and_leaves_scope_bound_knowledge():
    world, branch, place, witness = [uuid4() for _ in range(4)]
    state = WorldState(entities={
        place: WorldEntity(id=place, kind="place", name="Crossroads"),
        witness: WorldEntity(id=witness, kind="character", name="Courier", location_id=place),
    })
    policy = AmbientEventPolicy(scope_kind="region", scope_id=place, check_every_days=1, chance_basis_points=10_000)
    schedule = build_ambient_event_schedule(state, policy, first_tick=0, max_runs=2)
    state = state.model_copy(update={"scheduled_events": [schedule]})
    event = AmbientEventResolvedEvent(
        world_id=world, branch_id=branch, schedule_id=schedule.id, occurrence=0, checked_tick=0,
        event_id=ambient_event_id(schedule.id, branch, 0), roll=ambient_roll(str(world), branch, schedule),
        outcome="accepted", model_call_id="fixture-actual-call-id",
        proposal=AmbientEventProposal(kind="rumor", place_id=place, initiator_id=witness,
                                     title="A circulating rumor", description="A visitor says the evening ferry may be late."),
    )
    updated = apply_event(state, event)
    assert updated.scheduled_events[0].due_tick == state.calendar.day_seconds
    assert updated.ambient_events[0].event_id == event.event_id
    memory, = updated.knowledge
    assert memory.kind == "rumor" and memory.holder_id == witness
    assert memory.visibility.subject_ids == [witness]
    with pytest.raises(InvalidEvent, match="exact queued occurrence"):
        apply_event(updated, event)
    with pytest.raises(InvalidEvent, match="recorded roll"):
        apply_event(state, event.model_copy(update={"roll": (event.roll + 1) % 10_000}))


def test_player_only_sees_current_local_public_observations():
    place, other, player = [str(uuid4()) for _ in range(3)]
    entries = [
        ("public", "local_observation", place, 100),
        ("rumor", "rumor", place, 100),
        ("distant", "local_observation", other, 100),
        ("expired", "local_observation", place, 8),
    ]
    source = {
        "player_id": player, "current_tick": 10, "entities": {
            player: {"id": player, "kind": "character", "location_id": place, "components": {}},
            place: {"id": place, "kind": "place", "components": {}},
            other: {"id": other, "kind": "place", "components": {}},
        },
        "ambient_events": [
            {"event_id": ident, "start_tick": 0, "end_tick": end,
             "proposal": {"kind": kind, "place_id": location, "title": ident, "description": "a local observation"}}
            for ident, kind, location, end in entries
        ],
    }
    observations = project_player_state(source, player)["local_ambient_observations"]
    assert [row["id"] for row in observations] == ["public"]


def test_group_agenda_wakes_current_leader_without_committing_members_or_resources():
    world, branch, place, group, leader, member = [uuid4() for _ in range(6)]
    state = WorldState(entities={
        place: WorldEntity(id=place, kind="place", name="Workshop"),
        group: WorldEntity(id=group, kind="company", name="Guild", components={
            "governance": {"members": [str(leader), str(member)], "leader_id": str(leader)},
        }),
        leader: WorldEntity(id=leader, kind="character", name="Officer", location_id=place),
        member: WorldEntity(id=member, kind="character", name="Member", location_id=place),
    })
    policy = AmbientEventPolicy(scope_kind="organization", scope_id=group, check_every_days=1,
                                chance_basis_points=10_000)
    assert ambient_agenda_author_ids(state, policy) == {leader}
    schedule = build_ambient_event_schedule(state, policy, first_tick=0, max_runs=2)
    state = state.model_copy(update={"scheduled_events": [schedule]})
    proposal = AmbientEventProposal(
        kind="organization_agenda", place_id=place, initiator_id=leader, participant_ids=(member,),
        title="Review repairs", description="Check whether a temporary cover should be discussed.",
    )
    event = AmbientEventResolvedEvent(
        world_id=world, branch_id=branch, schedule_id=schedule.id, occurrence=0, checked_tick=0,
        event_id=ambient_event_id(schedule.id, branch, 0), roll=ambient_roll(str(world), branch, schedule),
        outcome="accepted", model_call_id="actual-call-id", proposal=proposal,
    )
    updated = apply_event(state, event)
    assert updated.resources == state.resources
    goal, = updated.goals
    assert (goal.owner_id, goal.counterpart_id) == (leader, member)
    assert any(row.payload.get("actor_id") == str(leader) for row in updated.scheduled_events)
    invalid = proposal.model_copy(update={"initiator_id": member, "participant_ids": (leader,)})
    with pytest.raises(InvalidEvent, match="current officer or leader"):
        apply_event(state, event.model_copy(update={"proposal": invalid}))
