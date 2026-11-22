from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.occupations import (
    OccupationActorContext,
    OccupationSource,
    WorkAssignment,
    WorkPosting,
    appoint_to_posting,
    occupation_assignment_id,
    resign_occupation,
)
from world_engine.domain.occupation_events import (
    OCCUPATION_APPOINTED_EVENT_TYPE,
    OCCUPATION_RESIGNED_EVENT_TYPE,
    OccupationAppointedEvent,
    OccupationResignedEvent,
    occupation_event_id,
    parse_occupation_event,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
EMPLOYER_ID = UUID("50000000-0000-4000-8000-000000000005")
LOCATION_ID = UUID("60000000-0000-4000-8000-000000000006")
POSTING_ID = UUID("70000000-0000-4000-8000-000000000007")
RULESET = "occupation-test.v1"
TICK = 321


def _actor(actor_id=ACTOR_ID):
    return OccupationActorContext(actor_id=actor_id, active=True, co_located=True)


def _posting(posting_id=POSTING_ID):
    return WorkPosting(
        posting_id=posting_id,
        employer_id=EMPLOYER_ID,
        location_id=LOCATION_ID,
        title="仓库管理员",
        slots=1,
        valid_from_tick=0,
        revision=4,
    )


def _appointment(*, event_id=None, actor_id=ACTOR_ID, posting_id=POSTING_ID, tick=TICK):
    command_id = COMMAND_ID
    event_id = event_id or occupation_event_id(
        command_id, RULESET, "appointed", actor_id, posting_id,
    )
    source = OccupationSource(kind="event", reference_id=str(event_id))
    assignment = appoint_to_posting(
        _posting(posting_id),
        (),
        actor=_actor(actor_id),
        expected_posting_revision=4,
        effective_tick=tick,
        source=source,
    )
    event = OccupationAppointedEvent(
        event_id=event_id,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET,
        actor_id=actor_id,
        effective_tick=tick,
        posting_id=posting_id,
        expected_posting_revision=4,
        resulting_assignment=assignment,
    )
    return event


def _resignation(*, source_event_id=None, assignment=None, actor_id=ACTOR_ID, tick=TICK + 1):
    appointment = _appointment(event_id=source_event_id) if assignment is None else None
    assignment = assignment or appointment.resulting_assignment
    event_id = occupation_event_id(
        COMMAND_ID, RULESET, "resigned", actor_id, assignment.assignment_id,
    )
    source = OccupationSource(kind="event", reference_id=str(event_id))
    resulting_assignment = resign_occupation(
        assignment,
        actor=_actor(actor_id),
        expected_assignment_revision=assignment.revision,
        effective_tick=tick,
        source=source,
    )
    event = OccupationResignedEvent(
        event_id=event_id,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        ruleset_version=RULESET,
        actor_id=actor_id,
        effective_tick=tick,
        assignment_id=assignment.assignment_id,
        expected_assignment_revision=assignment.revision,
        resulting_assignment=resulting_assignment,
    )
    return event, assignment


def test_appointment_event_binds_actor_posting_revision_tick_source_and_private_audience():
    event = _appointment()

    assert event.type == OCCUPATION_APPOINTED_EVENT_TYPE
    assert event.event_id == occupation_event_id(
        COMMAND_ID, RULESET, "appointed", ACTOR_ID, POSTING_ID,
    )
    assert event.expected_posting_revision == 4
    assert event.resulting_assignment.actor_id == event.actor_id
    assert event.resulting_assignment.posting_id == event.posting_id
    assert event.resulting_assignment.started_tick == event.effective_tick
    assert event.resulting_assignment.revision == 1
    assert event.resulting_assignment.status == "active"
    assert event.resulting_assignment.source == OccupationSource(
        kind="event", reference_id=str(event.event_id),
    )
    assert event.visibility == {"policy": "author_only"}
    assert parse_occupation_event(event.model_dump(mode="json")) == event


def test_appointment_event_id_is_stable_and_subject_bound():
    first = occupation_event_id(COMMAND_ID, RULESET, "appointed", ACTOR_ID, POSTING_ID)
    assert first == occupation_event_id(COMMAND_ID, RULESET, "appointed", ACTOR_ID, POSTING_ID)
    assert first != occupation_event_id(COMMAND_ID, RULESET, "appointed", uuid4(), POSTING_ID)
    assert first != occupation_event_id(COMMAND_ID, RULESET, "appointed", ACTOR_ID, uuid4())
    assert first != occupation_event_id(COMMAND_ID, "other-ruleset", "appointed", ACTOR_ID, POSTING_ID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", UUID("80000000-0000-4000-8000-000000000008")),
        ("posting_id", UUID("90000000-0000-4000-8000-000000000009")),
        ("effective_tick", TICK + 1),
        ("event_id", UUID("a0000000-0000-4000-8000-00000000000a")),
        ("visibility", {"policy": "public"}),
    ],
)
def test_appointment_event_rejects_mismatched_context_and_public_visibility(field, value):
    values = _appointment().model_dump(mode="python")
    values[field] = value
    with pytest.raises(ValidationError):
        OccupationAppointedEvent.model_validate(values)


def test_appointment_event_rejects_assignment_source_from_another_event():
    event = _appointment()
    wrong_source = OccupationSource(kind="event", reference_id=str(uuid4()))
    old_assignment = event.resulting_assignment
    replacement = WorkAssignment(
        **{
            **old_assignment.model_dump(mode="python"),
            "assignment_id": occupation_assignment_id(wrong_source, ACTOR_ID, POSTING_ID),
            "source": wrong_source,
        },
    )
    with pytest.raises(ValidationError):
        OccupationAppointedEvent.model_validate({
            **event.model_dump(mode="python"),
            "resulting_assignment": replacement,
        })


def test_resignation_event_binds_actor_assignment_revision_tick_and_end_source():
    event, original_assignment = _resignation()
    ended = event.resulting_assignment

    assert event.type == OCCUPATION_RESIGNED_EVENT_TYPE
    assert event.event_id == occupation_event_id(
        COMMAND_ID, RULESET, "resigned", ACTOR_ID, original_assignment.assignment_id,
    )
    assert event.expected_assignment_revision == original_assignment.revision
    assert ended.actor_id == event.actor_id
    assert ended.assignment_id == event.assignment_id
    assert ended.started_tick == original_assignment.started_tick
    assert ended.ended_tick == event.effective_tick
    assert ended.revision == event.expected_assignment_revision + 1
    assert ended.source == original_assignment.source
    assert ended.end_source == OccupationSource(kind="event", reference_id=str(event.event_id))
    assert event.visibility == {"policy": "author_only"}
    assert parse_occupation_event(event.model_dump(mode="json")) == event


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", UUID("b0000000-0000-4000-8000-00000000000b")),
        ("assignment_id", UUID("c0000000-0000-4000-8000-00000000000c")),
        ("effective_tick", TICK + 2),
        ("expected_assignment_revision", 9),
        ("event_id", UUID("d0000000-0000-4000-8000-00000000000d")),
        ("visibility", {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}),
    ],
)
def test_resignation_event_rejects_mismatched_context_and_visibility(field, value):
    event, _ = _resignation()
    values = event.model_dump(mode="python")
    values[field] = value
    with pytest.raises(ValidationError):
        OccupationResignedEvent.model_validate(values)


def test_resignation_event_requires_end_source_to_reference_its_event():
    event, _ = _resignation()
    ended = event.resulting_assignment
    replacement = WorkAssignment.model_validate({
        **ended.model_dump(mode="python"),
        "end_source": OccupationSource(kind="event", reference_id=str(uuid4())),
    })
    with pytest.raises(ValidationError):
        OccupationResignedEvent.model_validate({
            **event.model_dump(mode="python"),
            "resulting_assignment": replacement,
        })


def test_event_parser_rejects_unknown_occupation_event_types():
    values = _appointment().model_dump(mode="json")
    values["type"] = "core.occupation_appointed.v2"
    with pytest.raises(ValidationError):
        parse_occupation_event(values)
