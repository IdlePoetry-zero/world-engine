from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.relationship_reflection_events import (
    MEETING_REFLECTION_RECORDED_EVENT_TYPE,
    NPC_REFLECTION_COMMAND,
    MeetingReflectionRecordedEvent,
    meeting_reflection_event_id,
    meeting_reflection_visibility,
    parse_meeting_reflection_event,
    validate_meeting_reflection_batch,
)
from world_engine.domain.relationship_reflections import MeetingReflection


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
INVITATION_ID = UUID("50000000-0000-4000-8000-000000000005")
NPC_ID = UUID("60000000-0000-4000-8000-000000000006")
RULESET = "rules.v1"


def _reflection(**overrides) -> MeetingReflection:
    values = {
        "invitation_id": INVITATION_ID,
        "actor_id": ACTOR_ID,
        "outlook": "closer",
        "recorded_tick": 42,
    }
    return MeetingReflection.model_validate(values | overrides)


def _event(**overrides) -> MeetingReflectionRecordedEvent:
    values = {
        "event_id": meeting_reflection_event_id(COMMAND_ID, RULESET, INVITATION_ID),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": ACTOR_ID,
        "ruleset_version": RULESET,
        "reflection": _reflection(),
        "visibility": meeting_reflection_visibility(ACTOR_ID),
    }
    return MeetingReflectionRecordedEvent.model_validate(values | overrides)


def _scope(**overrides):
    values = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": ACTOR_ID,
        "ruleset_version": RULESET,
    }
    return values | overrides


def test_event_is_private_typed_frozen_and_round_trips():
    event = _event()

    assert event.type == MEETING_REFLECTION_RECORDED_EVENT_TYPE
    assert event.actor_id == event.reflection.actor_id == ACTOR_ID
    assert event.visibility == {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}
    assert parse_meeting_reflection_event(event.model_dump(mode="json")) == event
    assert event.requested_by is None
    assert "requested_by" not in event.model_dump(mode="python")
    assert "requested_by" not in event.model_dump(mode="json")
    assert '"requested_by"' not in event.model_dump_json()
    assert set(event.model_dump(mode="json")) == {
        "type", "event_id", "world_id", "branch_id", "command_id", "actor_id",
        "ruleset_version", "reflection", "visibility",
    }
    with pytest.raises(ValidationError):
        event.actor_id = uuid4()


def test_event_id_is_deterministic_and_bound_to_command_ruleset_and_invitation():
    event_id = meeting_reflection_event_id(COMMAND_ID, RULESET, INVITATION_ID)

    assert event_id == uuid5(COMMAND_ID, f"{RULESET}:meeting_reflection.recorded:{INVITATION_ID}")
    assert event_id == meeting_reflection_event_id(COMMAND_ID, RULESET, INVITATION_ID)
    assert event_id != meeting_reflection_event_id(uuid4(), RULESET, INVITATION_ID)
    assert event_id != meeting_reflection_event_id(COMMAND_ID, "rules.v2", INVITATION_ID)
    assert event_id != meeting_reflection_event_id(COMMAND_ID, RULESET, uuid4())
    with pytest.raises(ValueError, match="UUID command and invitation"):
        meeting_reflection_event_id("bad", RULESET, INVITATION_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        meeting_reflection_event_id(COMMAND_ID, " ", INVITATION_ID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", uuid4()),
        ("event_id", uuid4()),
        ("requested_by", ACTOR_ID),
        ("visibility", {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID), str(uuid4())]}),
        ("visibility", {"policy": "subject_ids", "subject_ids": []}),
        ("visibility", {"policy": "public", "subject_ids": [str(ACTOR_ID)]}),
        ("unexpected", True),
    ],
)
def test_event_rejects_actor_identity_and_visibility_forgery(field, value):
    values = _event().model_dump(mode="python")
    values[field] = value

    with pytest.raises(ValidationError):
        MeetingReflectionRecordedEvent.model_validate(values)


def test_reflection_actor_must_match_event_actor():
    with pytest.raises(ValidationError, match="must match the event actor"):
        _event(reflection=_reflection(actor_id=uuid4()))


def test_batch_binds_standalone_event_to_exact_command_payload():
    event = _event()
    payload = {
        "kind": "record_meeting_reflection",
        "invitation_id": str(INVITATION_ID),
        "outlook": "closer",
    }

    validate_meeting_reflection_batch(
        [event],
        command_type="action.record_meeting_reflection",
        command_payload=payload,
        **_scope(),
    )
    # Backup and historical replay validation has no original command proposal.
    validate_meeting_reflection_batch([event], **_scope())

    forged_payloads = (
        {**payload, "invitation_id": str(uuid4())},
        {**payload, "outlook": "uneasy"},
        {key: value for key, value in payload.items() if key != "outlook"},
        {**payload, "extra": True},
    )
    for forged_payload in forged_payloads:
        with pytest.raises(InvalidEvent, match="does not match its command proposal"):
            validate_meeting_reflection_batch(
                [event], command_type="action.record_meeting_reflection",
                command_payload=forged_payload, **_scope(),
            )


def test_batch_requires_event_and_rejects_nonstandalone_or_wrong_scope():
    event = _event()
    payload = {
        "kind": "record_meeting_reflection",
        "invitation_id": str(INVITATION_ID),
        "outlook": "closer",
    }
    with pytest.raises(InvalidEvent, match="requires its typed event"):
        validate_meeting_reflection_batch(
            [], command_type="action.record_meeting_reflection", command_payload=payload, **_scope(),
        )
    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_meeting_reflection_batch(
            [event, object()], command_type="action.record_meeting_reflection",
            command_payload=payload, **_scope(),
        )
    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_meeting_reflection_batch(
            [event], command_type="action.other", command_payload=payload, **_scope(actor_id=uuid4()),
        )
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_meeting_reflection_batch(
            [event], command_type="action.other", command_payload=payload, **_scope(),
        )


def test_requested_npc_reflection_binds_requester_and_never_accepts_proposed_outlook():
    event = _event(
        actor_id=NPC_ID,
        reflection=_reflection(actor_id=NPC_ID),
        visibility=meeting_reflection_visibility(NPC_ID),
        requested_by=ACTOR_ID,
    )
    payload = {
        "kind": "request_meeting_reflection",
        "invitation_id": str(INVITATION_ID),
    }
    request_scope = _scope(actor_id=ACTOR_ID)

    assert event.actor_id == event.reflection.actor_id == NPC_ID
    assert event.requested_by == ACTOR_ID
    assert event.visibility == {"policy": "subject_ids", "subject_ids": [str(NPC_ID)]}
    validate_meeting_reflection_batch(
        [event], command_type=NPC_REFLECTION_COMMAND, command_payload=payload, **request_scope,
    )
    # Restored events lack command metadata but must still bind to the requester.
    validate_meeting_reflection_batch([event], **request_scope)

    for forged_payload in (
        {**payload, "invitation_id": str(uuid4())},
        {**payload, "outlook": "closer"},
        {**payload, "extra": True},
        {"kind": "request_meeting_reflection"},
    ):
        with pytest.raises(InvalidEvent, match="does not match its command proposal"):
            validate_meeting_reflection_batch(
                [event], command_type=NPC_REFLECTION_COMMAND,
                command_payload=forged_payload, **request_scope,
            )
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_meeting_reflection_batch(
            [event], command_type="action.record_meeting_reflection",
            command_payload={
                "kind": "record_meeting_reflection",
                "invitation_id": str(INVITATION_ID),
                "outlook": "closer",
            }, **request_scope,
        )
    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_meeting_reflection_batch(
            [event], command_type=None, command_payload=None, **_scope(actor_id=NPC_ID),
        )
    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_meeting_reflection_batch(
            [event], command_type=NPC_REFLECTION_COMMAND, command_payload=payload,
            **_scope(actor_id=NPC_ID),
        )


def test_requested_by_requires_distinct_actor_and_both_request_commands_require_events():
    with pytest.raises(ValidationError, match="distinct actor"):
        _event(requested_by=ACTOR_ID)

    for command_type in ("action.record_meeting_reflection", NPC_REFLECTION_COMMAND):
        with pytest.raises(InvalidEvent, match="requires its typed event"):
            validate_meeting_reflection_batch(
                [], command_type=command_type, command_payload={}, **_scope(),
            )
