from uuid import UUID, uuid4

import pytest

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.occupation_legacy_vacancies import (
    OccupationLegacyVacancyError,
    apply_legacy_occupation_vacancies,
    occupation_legacy_vacancies_event_id,
    plan_legacy_occupation_vacancies,
)
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.reducers import apply_event


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
OWNER_ID = UUID("40000000-0000-4000-8000-000000000004")
DEAD_ID = UUID("50000000-0000-4000-8000-000000000005")
RETIRED_ID = UUID("60000000-0000-4000-8000-000000000006")
ACTIVE_ID = UUID("70000000-0000-4000-8000-000000000007")
ENDED_DEAD_ID = UUID("80000000-0000-4000-8000-000000000008")
EMPLOYER_ID = UUID("90000000-0000-4000-8000-000000000009")
PLACE_ID = UUID("a0000000-0000-4000-8000-00000000000a")
POSTING_ID = UUID("b0000000-0000-4000-8000-00000000000b")
RULESET = "1.4.0"
TICK = 5_000


def assignment(actor_id, *, revision=2, ended_tick=None):
    source = OccupationSource(kind="genesis", reference_id=f"pack.test:assignment:{actor_id}")
    end_source = (
        OccupationSource(kind="event", reference_id=str(uuid4())) if ended_tick is not None else None
    )
    return WorkAssignment(
        assignment_id=occupation_assignment_id(source, actor_id, POSTING_ID),
        actor_id=actor_id,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=PLACE_ID,
        title="Night clerk",
        started_tick=0,
        ended_tick=ended_tick,
        revision=revision,
        source=source,
        end_source=end_source,
    )


def world(*, include_dead=True):
    assignments = [
        assignment(DEAD_ID),
        assignment(RETIRED_ID),
        assignment(ACTIVE_ID),
        assignment(ENDED_DEAD_ID, ended_tick=TICK - 1),
    ]
    statuses = {
        DEAD_ID: "dead" if include_dead else "active",
        RETIRED_ID: "retired",
        ACTIVE_ID: "active",
        ENDED_DEAD_ID: "dead",
    }
    return WorldState(
        current_tick=TICK,
        entities={
            **{
                entity_id: WorldEntity(id=entity_id, kind="character", status=status)
                for entity_id, status in statuses.items()
            },
            EMPLOYER_ID: WorldEntity(id=EMPLOYER_ID, kind="organization"),
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place"),
        },
        work_postings=[WorkPosting(
            posting_id=POSTING_ID,
            employer_id=EMPLOYER_ID,
            location_id=PLACE_ID,
            title="Night clerk",
            slots=3,
            valid_from_tick=0,
        )],
        work_assignments=assignments,
    )


def plan(state=None, **context_changes):
    current = state or world()
    context = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": OWNER_ID,
        "ruleset_version": RULESET,
    }
    context.update(context_changes)
    return plan_legacy_occupation_vacancies(current, **context)


def test_legacy_plan_covers_only_current_dead_workers_with_active_assignments():
    state = world()

    event = plan(state)

    assert event is not None
    assert event.event_id == occupation_legacy_vacancies_event_id(COMMAND_ID, RULESET)
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        WORLD_ID, BRANCH_ID, COMMAND_ID, OWNER_ID,
    )
    assert event.effective_tick == TICK
    assert event.visibility == {"policy": "author_only"}
    assert len(event.vacancies) == 1
    vacancy, = event.vacancies
    assert vacancy.actor_id == DEAD_ID
    assert vacancy.assignment_id == next(row.assignment_id for row in state.work_assignments if row.actor_id == DEAD_ID)
    assert vacancy.expected_assignment_revision == 2
    assert RETIRED_ID not in {row.actor_id for row in event.vacancies}
    assert ACTIVE_ID not in {row.actor_id for row in event.vacancies}
    assert ENDED_DEAD_ID not in {row.actor_id for row in event.vacancies}


def test_no_event_is_planned_without_a_dead_worker_holding_an_active_assignment():
    state = world(include_dead=False)
    # The other historical dead worker's job was already ended.
    assert plan(state) is None


def test_apply_ends_exact_legacy_vacancies_and_uses_maintenance_event_as_source():
    state = world()
    event = plan(state)
    assert event is not None
    vacancy_assignment = next(row for row in state.work_assignments if row.actor_id == DEAD_ID)

    parsed = parse_event(event.model_dump(mode="json"))
    result = apply_event(state, parsed)

    ended = next(row for row in result.work_assignments if row.assignment_id == vacancy_assignment.assignment_id)
    assert ended.ended_tick == TICK
    assert ended.revision == vacancy_assignment.revision + 1
    assert ended.source == vacancy_assignment.source
    assert ended.end_source == OccupationSource(kind="event", reference_id=str(event.event_id))
    assert next(row for row in result.work_assignments if row.actor_id == RETIRED_ID).ended_tick is None
    assert next(row for row in result.work_assignments if row.actor_id == ACTIVE_ID).ended_tick is None
    assert next(row for row in result.work_assignments if row.actor_id == ENDED_DEAD_ID).ended_tick == TICK - 1
    assert next(row for row in state.work_assignments if row.actor_id == DEAD_ID).ended_tick is None


def test_apply_recomputes_manifest_and_rejects_stale_or_partial_cleanup():
    state = world()
    event = plan(state)
    assert event is not None
    forged = event.model_dump(mode="json")
    forged["vacancies"][0]["expected_assignment_revision"] += 1
    parsed_forgery = parse_event(forged)

    with pytest.raises(InvalidEvent, match="dead assignment holders"):
        apply_event(state, parsed_forgery)

    with pytest.raises(OccupationLegacyVacancyError) as raised:
        apply_legacy_occupation_vacancies(state.model_copy(update={"current_tick": TICK + 1}), event)
    assert raised.value.code == "stale_tick"


def test_event_id_is_command_and_ruleset_bound():
    event_id = occupation_legacy_vacancies_event_id(COMMAND_ID, RULESET)
    assert event_id == occupation_legacy_vacancies_event_id(COMMAND_ID, RULESET)
    assert event_id != occupation_legacy_vacancies_event_id(uuid4(), RULESET)
    assert event_id != occupation_legacy_vacancies_event_id(COMMAND_ID, "1.5.0")
