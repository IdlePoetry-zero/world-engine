from uuid import uuid4

import pytest

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import BackgroundDeprivationResolvedEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine


def _context(actor_id):
    return RuleContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(), actor_id=actor_id, ruleset_version="1.0.0"
    )


def _state_with_due_people(*, health: int, count: int = 1):
    actor_id, group_id = uuid4(), uuid4()
    people = {}
    for index in range(count):
        entity_id = uuid4()
        people[entity_id] = WorldEntity(
            id=entity_id,
            kind="character",
            name=f"Hungry neighbor {index}",
            components={
                "life": {"birth_tick": 0, "population_group_id": group_id},
                "health": {"current": health, "maximum": 100},
                "needs": {
                    "food": {"value": 10, "critical_below": 20, "last_critical_damage_tick": 86_400},
                    "water": {"value": 60, "critical_below": 20},
                    "rest": {"value": 60},
                },
            },
        )
    state = WorldState(
        current_tick=172_800,
        player_id=actor_id,
        entities={
            actor_id: WorldEntity(id=actor_id, kind="character", name="Observer"),
            group_id: WorldEntity(
                id=group_id,
                kind="group",
                name="Home region",
                components={
                    "population": {
                        "species_id": "core.human",
                        "headcount": count,
                        "unmodeled_headcount": 0,
                    }
                },
            ),
            **people,
        },
    )
    return state, actor_id, group_id, list(people)


def test_due_deprivation_batch_records_subject_and_atomically_decrements_population():
    state, actor_id, group_id, people = _state_with_due_people(health=1)
    context = _context(actor_id)

    resolution = RuleEngine().resolve_background_deprivation(state, context)

    assert resolution.outcome.status == "partial"
    assert resolution.outcome.code == "background_deprivation_death"
    event = resolution.events[0]
    assert isinstance(event, BackgroundDeprivationResolvedEvent)
    assert len(event.subjects) == 1
    assert event.subjects[0].entity_id == people[0]
    assert event.subjects[0].terminal_status == "dead"
    assert event.subjects[0].boundary_tick == state.current_tick

    result = apply_event(state, event)
    assert result.entities[people[0]].status == "dead"
    assert result.entities[people[0]].components["health"]["current"] == 0
    assert result.entities[group_id].components["population"]["headcount"] == 0
    assert result.entities[group_id].components["population"]["revision"] == 2


def test_same_population_deaths_are_one_cohort_revision_and_all_subjects_are_explicit():
    state, actor_id, group_id, people = _state_with_due_people(health=1, count=3)
    resolution = RuleEngine().resolve_background_deprivation(state, _context(actor_id))

    event = resolution.events[0]
    assert isinstance(event, BackgroundDeprivationResolvedEvent)
    assert [item.entity_id for item in event.subjects] == sorted(people, key=str)
    assert {item.expected_population_revision for item in event.subjects} == {1}
    result = apply_event(state, event)
    population = result.entities[group_id].components["population"]
    assert population["headcount"] == 0
    assert population["revision"] == 2
    assert all(result.entities[person_id].status == "dead" for person_id in people)


def test_deprivation_event_cannot_falsify_death_status_or_omit_due_subject():
    state, actor_id, _group_id, people = _state_with_due_people(health=1, count=2)
    event = RuleEngine().resolve_background_deprivation(state, _context(actor_id)).events[0]
    assert isinstance(event, BackgroundDeprivationResolvedEvent)

    false_terminal = event.model_copy(update={
        "subjects": [event.subjects[0].model_copy(update={"terminal_status": "active"}), event.subjects[1]]
    })
    with pytest.raises(InvalidEvent, match="terminal status"):
        apply_event(state, false_terminal)
    omitted = event.model_copy(update={"subjects": event.subjects[:1]})
    with pytest.raises(InvalidEvent, match="every due active nonacting subject"):
        apply_event(state, omitted)


def test_world_advance_deprivation_accepts_owner_actor_and_includes_all_due_people():
    state, _player_id, group_id, people = _state_with_due_people(health=1, count=2)
    owner_actor_id = uuid4()
    resolution = RuleEngine().resolve_background_deprivation(
        state,
        _context(owner_actor_id),
        include_actor=True,
    )

    event = resolution.events[0]
    assert isinstance(event, BackgroundDeprivationResolvedEvent)
    assert event.actor_id == owner_actor_id
    assert event.include_actor is True
    assert [item.entity_id for item in event.subjects] == sorted(people, key=str)
    result = apply_event(state, event)
    assert all(result.entities[person_id].status == "dead" for person_id in people)
    assert result.entities[group_id].components["population"]["headcount"] == 0
