from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.gameplay_events import PopulationYearAdvancedEvent
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine


YEAR_SECONDS = 360 * 86_400


def _age_population(*, age_199: int = 0, age_200: int = 0, birth_remainder: str = "0", death_remainder: str = "0"):
    return {
        "species_id": "core.human",
        "headcount": age_199 + age_200,
        "unmodeled_headcount": age_199 + age_200,
        "last_advanced_year": 0,
        "age_rates": [
            {"age_years": age, "annual_birth_rate": "0", "annual_death_rate": "0"}
            for age in range(201)
        ],
        "age_cohorts": [
            {
                "age_years": age,
                "headcount": age_199 if age == 199 else age_200 if age == 200 else 0,
                "birth_remainder": birth_remainder if age in {199, 200} else "0",
                "death_remainder": death_remainder if age in {199, 200} else "0",
            }
            for age in range(201)
        ],
    }


def _state_with_schedule(populations):
    actor_id, schedule_id = uuid4(), uuid4()
    group_entities = {
        group_id: WorldEntity(
            id=group_id,
            kind="group",
            name=f"Region {index}",
            components={"population": population},
        )
        for index, (group_id, population) in enumerate(populations)
    }
    state = WorldState(
        player_id=actor_id,
        entities={
            actor_id: WorldEntity(id=actor_id, kind="character", name="Observer"),
            **group_entities,
        },
        scheduled_events=[ScheduledEventRecord(
            id=schedule_id,
            due_tick=YEAR_SECONDS,
            phase="environment",
            handler_id="population.year_advance",
            handler_version="1.0.0",
            repeat_ticks=YEAR_SECONDS,
            max_runs=2,
        )],
    )
    return state, actor_id, schedule_id


def _context(actor_id):
    return RuleContext(
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _resolve_first_year(state, actor_id, schedule_id):
    advanced = apply_event(state, TimeAdvancedEvent(to_tick=YEAR_SECONDS))
    schedule = advanced.scheduled_events[0]
    resolution = RuleEngine().resolve_scheduled(advanced, schedule, _context(actor_id))
    assert resolution.outcome.status == "succeeded"
    assert resolution.outcome.code == "population_year_advanced"
    assert resolution.outcome.details["year_index"] == 1
    assert resolution.outcome.details["cohort_count"] == len(state.entities) - 1
    population_events = [event for event in resolution.events if isinstance(event, PopulationYearAdvancedEvent)]
    assert len(population_events) == 1
    assert population_events[0].scheduled_event_id == schedule_id
    result = advanced
    for event in resolution.events:
        result = apply_event(result, event)
    return result, resolution


def test_population_year_schedule_resolves_all_groups_as_one_typed_event():
    first_id, second_id = uuid4(), uuid4()
    state, actor_id, schedule_id = _state_with_schedule([
        (first_id, _age_population(age_199=3)),
        (second_id, _age_population(age_200=5)),
    ])

    result, resolution = _resolve_first_year(state, actor_id, schedule_id)

    assert len([event for event in resolution.events if isinstance(event, PopulationYearAdvancedEvent)]) == 1
    expected_ids = [item.cohort_id for item in resolution.events[0].expected_populations]
    assert expected_ids == sorted([first_id, second_id], key=str)
    assert result.entities[first_id].components["population"]["last_advanced_year"] == 1
    assert result.entities[second_id].components["population"]["age_cohorts"][200]["headcount"] == 5
    assert result.scheduled_events[0].due_tick == 2 * YEAR_SECONDS
    assert result.scheduled_events[0].completed_runs == 1


def test_200_plus_cohort_merges_fractional_remainders_without_losing_them():
    group_id = uuid4()
    state, actor_id, schedule_id = _state_with_schedule([
        (group_id, _age_population(age_199=1, age_200=1, birth_remainder="0.75", death_remainder="0.75")),
    ])

    first, _ = _resolve_first_year(state, actor_id, schedule_id)
    population = first.entities[group_id].components["population"]
    assert population["age_cohorts"][200]["headcount"] == 2
    assert Decimal(population["age_cohorts"][200]["birth_remainder"]) == Decimal("1.5")
    assert Decimal(population["age_cohorts"][200]["death_remainder"]) == Decimal("1.5")
    assert population["headcount"] == population["unmodeled_headcount"] == 2

    second_tick = 2 * YEAR_SECONDS
    second_state = apply_event(first, TimeAdvancedEvent(to_tick=second_tick))
    second_resolution = RuleEngine().resolve_scheduled(
        second_state,
        second_state.scheduled_events[0],
        _context(actor_id),
    )
    result = second_state
    for event in second_resolution.events:
        result = apply_event(result, event)
    population = result.entities[group_id].components["population"]
    assert population["age_cohorts"][0]["headcount"] == 1
    assert population["age_cohorts"][200]["headcount"] == 1
    assert population["headcount"] == population["unmodeled_headcount"] == 2
    assert population["age_cohorts"][0]["revision"] == 3


def test_population_year_schedule_rejects_nonannual_interval():
    state, actor_id, _schedule_id = _state_with_schedule([(uuid4(), _age_population(age_199=1))])
    advanced = apply_event(state, TimeAdvancedEvent(to_tick=YEAR_SECONDS))
    bad_schedule = advanced.scheduled_events[0].model_copy(update={"repeat_ticks": YEAR_SECONDS // 2})
    invalid_state = WorldState.model_validate({
        **advanced.model_dump(mode="json"),
        "scheduled_events": [bad_schedule.model_dump(mode="json")],
    })

    resolution = RuleEngine().resolve_scheduled(invalid_state, bad_schedule, _context(actor_id))

    assert resolution.outcome.status == "rejected"
    assert resolution.outcome.code == "population_schedule_mismatch"
