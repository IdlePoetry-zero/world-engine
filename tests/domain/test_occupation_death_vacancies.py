from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.occupation_death_vacancies import (
    OccupationDeathVacancy,
    OccupationDeathVacancyError,
    OccupationDeathVacanciesResolvedEvent,
    apply_occupation_death_vacancies,
    occupation_death_vacancies_event_id,
    plan_occupation_death_vacancies,
)
from world_engine.domain.occupations import OccupationSource, WorkAssignment, occupation_assignment_id


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
COMMAND_ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
WORKER_A_ID = UUID("50000000-0000-4000-8000-000000000005")
WORKER_B_ID = UUID("60000000-0000-4000-8000-000000000006")
POSTING_ID = UUID("70000000-0000-4000-8000-000000000007")
TERMINAL_A_ID = UUID("80000000-0000-4000-8000-000000000008")
TERMINAL_B_ID = UUID("90000000-0000-4000-8000-000000000009")
RULESET = "1.4.0"
TICK = 86_400


def assignment(actor_id=WORKER_A_ID, *, source_id=None, started_tick=0, revision=4, ended_tick=None):
    source = OccupationSource(kind="genesis", reference_id=f"pack.test.assignment:{actor_id}")
    row_id = occupation_assignment_id(source, actor_id, POSTING_ID)
    end_source = None
    if ended_tick is not None:
        end_source = OccupationSource(kind="event", reference_id=str(source_id or uuid4()))
    return WorkAssignment(
        assignment_id=row_id,
        actor_id=actor_id,
        posting_id=POSTING_ID,
        employer_id=UUID("a0000000-0000-4000-8000-00000000000a"),
        location_id=UUID("b0000000-0000-4000-8000-00000000000b"),
        title="Archivist",
        started_tick=started_tick,
        ended_tick=ended_tick,
        revision=revision,
        source=source,
        end_source=end_source,
    )


def plan(assignments, causes=None, *, effective_tick=TICK):
    return plan_occupation_death_vacancies(
        assignments,
        terminal_transition_event_by_actor=causes or {},
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=COMMAND_ACTOR_ID,
        ruleset_version=RULESET,
        effective_tick=effective_tick,
    )


def test_event_id_is_stable_and_bound_to_command_and_ruleset():
    original = occupation_death_vacancies_event_id(COMMAND_ID, RULESET)
    assert original == occupation_death_vacancies_event_id(COMMAND_ID, RULESET)
    assert original != occupation_death_vacancies_event_id(uuid4(), RULESET)
    assert original != occupation_death_vacancies_event_id(COMMAND_ID, "1.5.0")


def test_planner_returns_none_when_no_newly_terminal_worker_holds_a_job():
    rows = [assignment(WORKER_A_ID)]
    assert plan(rows, {WORKER_B_ID: TERMINAL_B_ID}) is None
    assert plan(rows, {}) is None
    assert rows[0].ended_tick is None


def test_planner_builds_sorted_author_private_cas_manifest_for_newly_terminal_workers():
    first, second = assignment(WORKER_A_ID), assignment(WORKER_B_ID, revision=7)
    causes = {WORKER_A_ID: TERMINAL_A_ID, WORKER_B_ID: TERMINAL_B_ID}

    event = plan([second, first], causes)

    assert isinstance(event, OccupationDeathVacanciesResolvedEvent)
    assert event.event_id == occupation_death_vacancies_event_id(COMMAND_ID, RULESET)
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        WORLD_ID, BRANCH_ID, COMMAND_ID, COMMAND_ACTOR_ID,
    )
    assert event.effective_tick == TICK
    assert event.visibility == {"policy": "author_only"}
    assert [item.assignment_id for item in event.closures] == sorted(
        [first.assignment_id, second.assignment_id], key=str,
    )
    by_worker = {item.actor_id: item for item in event.closures}
    assert by_worker[WORKER_A_ID].terminal_event_id == TERMINAL_A_ID
    assert by_worker[WORKER_A_ID].expected_assignment_revision == 4
    assert by_worker[WORKER_B_ID].terminal_event_id == TERMINAL_B_ID
    assert by_worker[WORKER_B_ID].expected_assignment_revision == 7


def test_same_batch_terminal_event_may_cause_multiple_worker_vacancies():
    cause = uuid4()
    event = plan(
        [assignment(WORKER_A_ID), assignment(WORKER_B_ID)],
        {WORKER_A_ID: cause, WORKER_B_ID: cause},
    )
    assert event is not None
    assert {item.terminal_event_id for item in event.closures} == {cause}


def test_apply_ends_only_matching_assignments_and_preserves_original_provenance():
    active_a = assignment(WORKER_A_ID, revision=4)
    active_b = assignment(WORKER_B_ID, revision=2)
    already_ended = assignment(UUID("c0000000-0000-4000-8000-00000000000c"), ended_tick=10)
    rows = [already_ended, active_b, active_a]
    causes = {WORKER_A_ID: TERMINAL_A_ID, WORKER_B_ID: TERMINAL_B_ID}
    event = plan(rows, causes)
    assert event is not None

    result = apply_occupation_death_vacancies(
        rows, event, terminal_transition_event_by_actor=causes, current_tick=TICK,
    )

    by_id = {row.assignment_id: row for row in result}
    for prior, cause in ((active_a, TERMINAL_A_ID), (active_b, TERMINAL_B_ID)):
        ended = by_id[prior.assignment_id]
        assert ended.assignment_id == prior.assignment_id
        assert ended.source == prior.source
        assert ended.actor_id == prior.actor_id
        assert ended.started_tick == prior.started_tick
        assert ended.ended_tick == TICK
        assert ended.revision == prior.revision + 1
        assert ended.end_source == OccupationSource(kind="event", reference_id=str(cause))
    assert by_id[already_ended.assignment_id] == already_ended
    assert all(row.ended_tick is not None for row in result)
    assert all(row.ended_tick is None for row in rows if row is not already_ended)


def test_apply_rejects_missing_or_forged_causal_terminal_reference():
    rows = [assignment()]
    event = plan(rows, {WORKER_A_ID: TERMINAL_A_ID})
    assert event is not None

    with pytest.raises(OccupationDeathVacancyError) as raised:
        apply_occupation_death_vacancies(rows, event, terminal_transition_event_by_actor={}, current_tick=TICK)
    assert raised.value.code == "closure_manifest_mismatch"

    wrong_source = {WORKER_A_ID: TERMINAL_B_ID}
    with pytest.raises(OccupationDeathVacancyError) as raised:
        apply_occupation_death_vacancies(
            rows, event, terminal_transition_event_by_actor=wrong_source, current_tick=TICK,
        )
    assert raised.value.code == "closure_manifest_mismatch"


def test_apply_rejects_stale_assignment_revision_and_tick():
    original = assignment(revision=4)
    event = plan([original], {WORKER_A_ID: TERMINAL_A_ID})
    assert event is not None
    newer = assignment(revision=5)

    with pytest.raises(OccupationDeathVacancyError) as raised:
        apply_occupation_death_vacancies(
            [newer], event, terminal_transition_event_by_actor={WORKER_A_ID: TERMINAL_A_ID}, current_tick=TICK,
        )
    assert raised.value.code == "closure_manifest_mismatch"
    with pytest.raises(OccupationDeathVacancyError) as raised:
        apply_occupation_death_vacancies(
            [original], event, terminal_transition_event_by_actor={WORKER_A_ID: TERMINAL_A_ID}, current_tick=TICK + 1,
        )
    assert raised.value.code == "stale_tick"


def test_planner_rejects_assignments_started_after_death_tick_and_revision_overflow():
    with pytest.raises(OccupationDeathVacancyError) as raised:
        plan([assignment(started_tick=TICK + 1)], {WORKER_A_ID: TERMINAL_A_ID})
    assert raised.value.code == "assignment_not_started"

    with pytest.raises(OccupationDeathVacancyError) as raised:
        plan([assignment(revision=2**63 - 1)], {WORKER_A_ID: TERMINAL_A_ID})
    assert raised.value.code == "assignment_revision_exhausted"


def test_event_contract_rejects_noncanonical_rows_visibility_and_context():
    rows = [assignment(WORKER_A_ID), assignment(WORKER_B_ID)]
    event = plan(rows, {WORKER_A_ID: TERMINAL_A_ID, WORKER_B_ID: TERMINAL_B_ID})
    assert event is not None
    dumped = event.model_dump(mode="python")

    reversed_rows = dict(dumped)
    reversed_rows["closures"] = list(reversed(dumped["closures"]))
    with pytest.raises(ValidationError, match="sorted"):
        OccupationDeathVacanciesResolvedEvent.model_validate(reversed_rows)

    duplicate_rows = dict(dumped)
    duplicate_rows["closures"] = [dumped["closures"][0], dumped["closures"][0]]
    with pytest.raises(ValidationError, match="sorted"):
        OccupationDeathVacanciesResolvedEvent.model_validate(duplicate_rows)

    public = dict(dumped)
    public["visibility"] = {"policy": "public"}
    with pytest.raises(ValidationError, match="author_only"):
        OccupationDeathVacanciesResolvedEvent.model_validate(public)

    bad_event_id = dict(dumped, event_id=uuid4())
    with pytest.raises(ValidationError, match="bound to its command"):
        OccupationDeathVacanciesResolvedEvent.model_validate(bad_event_id)


def test_apply_rejects_event_not_covering_each_affected_assignment():
    first = assignment(WORKER_A_ID)
    second = assignment(WORKER_B_ID)
    causes = {WORKER_A_ID: TERMINAL_A_ID, WORKER_B_ID: TERMINAL_B_ID}
    complete = plan([first, second], causes)
    assert complete is not None
    partial = complete.model_copy(update={"closures": complete.closures[:1]})

    with pytest.raises(OccupationDeathVacancyError) as raised:
        apply_occupation_death_vacancies([first, second], partial, terminal_transition_event_by_actor=causes,
                                         current_tick=TICK)
    assert raised.value.code == "closure_manifest_mismatch"


def test_terminal_cause_map_must_use_uuid_keys_and_values():
    with pytest.raises(OccupationDeathVacancyError) as raised:
        plan([assignment()], {"not-a-uuid": TERMINAL_A_ID})
    assert raised.value.code == "invalid_terminal_map"


def test_row_contract_is_closed_to_revisionless_or_nonuuid_sources():
    with pytest.raises(ValidationError):
        OccupationDeathVacancy(
            assignment_id=uuid4(), actor_id=uuid4(), terminal_event_id="untrusted", expected_assignment_revision=1,
        )
