from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import SkillValue, SkillsComponent
from world_engine.domain.occupations import (
    OccupationActorContext,
    OccupationError,
    OccupationSkillRequirement,
    OccupationSource,
    WorkAssignment,
    WorkPosting,
    appoint_to_posting,
    occupation_assignment_id,
    resign_occupation,
)


ACTOR_ID = UUID("10000000-0000-4000-8000-000000000001")
EMPLOYER_ID = UUID("20000000-0000-4000-8000-000000000002")
LOCATION_ID = UUID("30000000-0000-4000-8000-000000000003")
POSTING_ID = UUID("40000000-0000-4000-8000-000000000004")
OTHER_POSTING_ID = UUID("50000000-0000-4000-8000-000000000005")


def _source(event_id=None):
    return OccupationSource(kind="event", reference_id=str(event_id or uuid4()))


def _posting(**changes):
    values = {
        "posting_id": POSTING_ID,
        "employer_id": EMPLOYER_ID,
        "location_id": LOCATION_ID,
        "title": "药圃照料",
        "required_skill": OccupationSkillRequirement(skill_id="skill.care", minimum_level="4"),
        "slots": 2,
        "valid_from_tick": 100,
        "valid_until_tick": 200,
        "status": "open",
        "revision": 3,
    }
    values.update(changes)
    return WorkPosting(**values)


def _actor(*, actor_id=ACTOR_ID, active=True, co_located=True, level="5"):
    skills = SkillsComponent(
        skills={"skill.care": SkillValue(level=level)} if level is not None else {},
    )
    return OccupationActorContext(
        actor_id=actor_id,
        active=active,
        co_located=co_located,
        skills=skills,
    )


def _appoint(*, posting=None, assignments=(), actor=None, source=None, tick=120, expected_revision=3):
    return appoint_to_posting(
        posting or _posting(),
        assignments,
        actor=actor or _actor(),
        expected_posting_revision=expected_revision,
        effective_tick=tick,
        source=source or _source(),
    )


def test_appointment_snapshots_posting_and_existing_skill_without_mutating_inputs():
    posting = _posting()
    source = _source()
    assignment = _appoint(posting=posting, source=source)

    assert assignment.assignment_id == occupation_assignment_id(source, ACTOR_ID, POSTING_ID)
    assert assignment.actor_id == ACTOR_ID
    assert assignment.posting_id == POSTING_ID
    assert assignment.employer_id == EMPLOYER_ID
    assert assignment.location_id == LOCATION_ID
    assert assignment.title == posting.title
    assert assignment.required_skill == posting.required_skill
    assert assignment.started_tick == 120
    assert assignment.ended_tick is None
    assert assignment.status == "active"
    assert assignment.revision == 1
    assert assignment.source == source
    assert "payment" not in WorkAssignment.model_fields
    assert "duration_seconds" not in WorkAssignment.model_fields
    assert posting.revision == 3


def test_assignment_id_is_stable_for_the_same_event_and_endpoints():
    source = _source()
    assert occupation_assignment_id(source, ACTOR_ID, POSTING_ID) == occupation_assignment_id(
        source, ACTOR_ID, POSTING_ID,
    )
    assert occupation_assignment_id(source, ACTOR_ID, POSTING_ID) != occupation_assignment_id(
        source, ACTOR_ID, OTHER_POSTING_ID,
    )
    assert _appoint(source=source).assignment_id == _appoint(source=source).assignment_id


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"expected_revision": 2}, "stale_posting_revision"),
        ({"actor": _actor(active=False)}, "actor_inactive"),
        ({"actor": _actor(co_located=False)}, "actor_not_at_workplace"),
        ({"actor": _actor(level="3")}, "required_skill_missing"),
        ({"actor": _actor(level=None)}, "required_skill_missing"),
        ({"posting": _posting(status="closed")}, "posting_closed"),
        ({"tick": 99}, "posting_out_of_date"),
        ({"tick": 200}, "posting_out_of_date"),
    ],
)
def test_appointment_checks_trusted_context_posting_revision_and_validity(kwargs, code):
    with pytest.raises(OccupationError) as raised:
        _appoint(**kwargs)
    assert raised.value.code == code


def test_appointment_enforces_one_active_job_per_actor_and_posting_slots():
    existing_for_actor = _appoint(posting=_posting(posting_id=OTHER_POSTING_ID))
    with pytest.raises(OccupationError, match="already has an active job") as raised:
        _appoint(assignments=[existing_for_actor])
    assert raised.value.code == "actor_already_employed"

    incumbent = _appoint(actor=_actor(actor_id=UUID("60000000-0000-4000-8000-000000000006")))
    with pytest.raises(OccupationError, match="no remaining slots") as raised:
        _appoint(posting=_posting(slots=1), assignments=[incumbent])
    assert raised.value.code == "posting_full"


def test_ended_assignments_do_not_occupy_slots_or_block_a_new_job():
    original = _appoint(posting=_posting(slots=1))
    ended = resign_occupation(
        original,
        actor=_actor(),
        expected_assignment_revision=1,
        effective_tick=130,
        source=_source(),
    )
    other_actor = _actor(actor_id=UUID("70000000-0000-4000-8000-000000000007"))
    replacement = _appoint(
        posting=_posting(slots=1),
        assignments=[ended],
        actor=other_actor,
    )

    assert replacement.actor_id == other_actor.actor_id
    assert original.status == "active"
    assert ended.status == "ended"


def test_resignation_preserves_assignment_identity_and_allows_remote_exit():
    appointment_source = _source()
    assignment = _appoint(source=appointment_source)
    resignation_source = _source()
    ended = resign_occupation(
        assignment,
        actor=_actor(co_located=False),
        expected_assignment_revision=1,
        effective_tick=145,
        source=resignation_source,
    )

    assert ended.assignment_id == assignment.assignment_id
    assert ended.source == appointment_source
    assert ended.end_source == resignation_source
    assert ended.started_tick == 120
    assert ended.ended_tick == 145
    assert ended.revision == 2
    assert ended.status == "ended"
    assert assignment.ended_tick is None


@pytest.mark.parametrize(
    ("actor", "expected_revision", "tick", "code"),
    [
        (_actor(active=False), 1, 130, "actor_inactive"),
        (_actor(actor_id=UUID("80000000-0000-4000-8000-000000000008")), 1, 130, "not_assignment_holder"),
        (_actor(), 2, 130, "stale_assignment_revision"),
        (_actor(), 1, 119, "tick_precedes_assignment"),
    ],
)
def test_resignation_checks_actor_revision_and_effective_time(actor, expected_revision, tick, code):
    assignment = _appoint()
    with pytest.raises(OccupationError) as raised:
        resign_occupation(
            assignment,
            actor=actor,
            expected_assignment_revision=expected_revision,
            effective_tick=tick,
            source=_source(),
        )
    assert raised.value.code == code


def test_resignation_rejects_closed_assignment_and_reused_appointment_event():
    assignment = _appoint()
    ended = resign_occupation(
        assignment,
        actor=_actor(),
        expected_assignment_revision=1,
        effective_tick=130,
        source=_source(),
    )
    with pytest.raises(OccupationError) as raised:
        resign_occupation(
            ended,
            actor=_actor(),
            expected_assignment_revision=2,
            effective_tick=140,
            source=_source(),
        )
    assert raised.value.code == "assignment_not_active"

    with pytest.raises(OccupationError) as raised:
        resign_occupation(
            assignment,
            actor=_actor(),
            expected_assignment_revision=1,
            effective_tick=130,
            source=assignment.source,
        )
    assert raised.value.code == "source_event_reused"


def test_live_changes_require_event_sources_only():
    authored_source = OccupationSource(kind="pack", reference_id="modern_city:2.2.0")
    with pytest.raises(OccupationError) as raised:
        _appoint(source=authored_source)
    assert raised.value.code == "event_source_required"

    with pytest.raises(ValidationError):
        OccupationSource(kind="event", reference_id="not-a-uuid")


def test_models_reject_invalid_posting_interval_and_assignment_identity():
    with pytest.raises(ValidationError):
        _posting(valid_until_tick=100)

    source = _source()
    with pytest.raises(ValidationError, match="assignment ID"):
        WorkAssignment(
            assignment_id=uuid4(), actor_id=ACTOR_ID, posting_id=POSTING_ID,
            employer_id=EMPLOYER_ID, location_id=LOCATION_ID, title="药圃照料",
            required_skill=None, started_tick=100, source=source,
        )
