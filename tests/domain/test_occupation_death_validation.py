from uuid import UUID, uuid4

import pytest

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import DamageAppliedEvent
from world_engine.domain.models import EntityFieldChangedEvent, WorldEntity, WorldState
from world_engine.domain.occupation_death_validation import (
    derive_death_vacancies,
    validate_persisted_death_vacancies,
)
from world_engine.domain.occupation_death_vacancies import plan_occupation_death_vacancies
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.events import parse_event


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
COMMAND_ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
WORKER_ID = UUID("50000000-0000-4000-8000-000000000005")
EMPLOYER_ID = UUID("60000000-0000-4000-8000-000000000006")
PLACE_ID = UUID("70000000-0000-4000-8000-000000000007")
POSTING_ID = UUID("80000000-0000-4000-8000-000000000008")
NOW = 10_000
RULESET = "1.4.0"


def state_with_worker(*, health=10, status="active"):
    source = OccupationSource(kind="genesis", reference_id="pack.test:initial-assignment")
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, WORKER_ID, POSTING_ID),
        actor_id=WORKER_ID,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=PLACE_ID,
        title="Registry clerk",
        started_tick=0,
        revision=3,
        source=source,
    )
    return WorldState(
        current_tick=NOW,
        entities={
            WORKER_ID: WorldEntity(
                id=WORKER_ID,
                kind="character",
                status=status,
                components={"health": {"current": health, "maximum": 10}},
            ),
            EMPLOYER_ID: WorldEntity(id=EMPLOYER_ID, kind="organization"),
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place"),
        },
        work_postings=[WorkPosting(
            posting_id=POSTING_ID,
            employer_id=EMPLOYER_ID,
            location_id=PLACE_ID,
            title="Registry clerk",
            slots=1,
            valid_from_tick=0,
        )],
        work_assignments=[assignment],
    )


def context():
    return {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": COMMAND_ACTOR_ID,
        "ruleset_version": RULESET,
    }


def test_nonlethal_damage_does_not_release_assignment_slot():
    state = state_with_worker(health=10)
    damage = DamageAppliedEvent(
        target_id=WORKER_ID,
        damage=4,
        expected_health_revision=1,
    )

    vacancy = derive_death_vacancies(state, [damage], **context())

    assert vacancy is None


def test_lethal_damage_derives_vacancy_from_reducer_transition_and_validates_suffix():
    state = state_with_worker(health=5)
    damage = DamageAppliedEvent(
        target_id=WORKER_ID,
        damage=5,
        expected_health_revision=1,
    )

    vacancy = derive_death_vacancies(state, [damage], **context())

    assert vacancy is not None
    assert len(vacancy.closures) == 1
    closure, = vacancy.closures
    assert closure.actor_id == WORKER_ID
    assert closure.terminal_event_id == damage.event_id
    assert closure.expected_assignment_revision == 3
    assert vacancy.effective_tick == NOW
    persisted = [parse_event(damage.model_dump(mode="json")), parse_event(vacancy.model_dump(mode="json"))]
    assert validate_persisted_death_vacancies(state, persisted, **context()) == persisted[:1]


def test_explicit_status_death_derives_vacancy_from_actual_field_transition():
    state = state_with_worker(health=10)
    death = EntityFieldChangedEvent(
        entity_id=WORKER_ID,
        field_name="status",
        value="dead",
    )

    vacancy = derive_death_vacancies(state, [death], **context())

    assert vacancy is not None
    assert vacancy.closures[0].terminal_event_id == death.event_id
    persisted = [parse_event(death.model_dump(mode="json")), parse_event(vacancy.model_dump(mode="json"))]
    assert validate_persisted_death_vacancies(state, persisted, **context()) == persisted[:1]


def test_status_change_to_nonterminal_status_cannot_support_forged_vacancy():
    state = state_with_worker(health=10)
    rename_status = EntityFieldChangedEvent(
        entity_id=WORKER_ID,
        field_name="status",
        value="retired",
    )
    forged = plan_occupation_death_vacancies(
        state.work_assignments,
        terminal_transition_event_by_actor={WORKER_ID: rename_status.event_id},
        **context(),
        effective_tick=NOW,
    )
    assert forged is not None
    persisted = [parse_event(rename_status.model_dump(mode="json")), parse_event(forged.model_dump(mode="json"))]

    with pytest.raises(InvalidEvent, match="match deaths and assignments"):
        validate_persisted_death_vacancies(state, persisted, **context())


def test_forged_suffix_cannot_relabel_a_real_lethal_event_as_another_cause():
    state = state_with_worker(health=5)
    damage = DamageAppliedEvent(
        target_id=WORKER_ID,
        damage=5,
        expected_health_revision=1,
    )
    derived = derive_death_vacancies(state, [damage], **context())
    assert derived is not None
    forged_payload = derived.model_dump(mode="json")
    forged_payload["closures"][0]["terminal_event_id"] = str(uuid4())
    forged = parse_event(forged_payload)

    with pytest.raises(InvalidEvent, match="match deaths and assignments"):
        validate_persisted_death_vacancies(
            state,
            [parse_event(damage.model_dump(mode="json")), forged],
            **context(),
        )


def test_forged_suffix_cannot_release_slot_after_nonlethal_damage():
    state = state_with_worker(health=10)
    damage = DamageAppliedEvent(
        target_id=WORKER_ID,
        damage=2,
        expected_health_revision=1,
    )
    forged = plan_occupation_death_vacancies(
        state.work_assignments,
        terminal_transition_event_by_actor={WORKER_ID: damage.event_id},
        **context(),
        effective_tick=NOW,
    )
    assert forged is not None

    with pytest.raises(InvalidEvent, match="match deaths and assignments"):
        validate_persisted_death_vacancies(
            state,
            [parse_event(damage.model_dump(mode="json")), parse_event(forged.model_dump(mode="json"))],
            **context(),
        )
