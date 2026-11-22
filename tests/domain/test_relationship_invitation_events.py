from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.relationship_invitations import RelationshipInvitation
from world_engine.domain.relationship_invitation_events import (
    RELATIONSHIP_INVITATION_CHANGED_EVENT_TYPE,
    RelationInvitationChangedEvent,
    invitation_event_id,
    parse_relationship_invitation_event,
    participant_visibility,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
PROPOSER_ID = UUID("40000000-0000-4000-8000-000000000004")
INVITEE_ID = UUID("50000000-0000-4000-8000-000000000005")
INVITATION_ID = UUID("60000000-0000-4000-8000-000000000006")
RULESET = "invitation-test.v1"


def _invitation(status: str, revision: int) -> RelationshipInvitation:
    return RelationshipInvitation(
        invitation_id=INVITATION_ID,
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        status=status,
        proposed_tick=10,
        updated_tick=10 if status == "pending" else 11,
        revision=revision,
        meeting={"start_tick": 12, "end_tick": 20} if status == "accepted" else None,
        meeting_attendance=(
            [{"actor_id": INVITEE_ID, "attended_tick": 12}]
            if status == "accepted" and revision == 3
            else []
        ),
    )


def _event(operation: str) -> RelationInvitationChangedEvent:
    expected_revision, status, revision = {
        "propose": (0, "pending", 1),
        "accept": (1, "accepted", 2),
        "decline": (1, "declined", 2),
        "withdraw": (1, "withdrawn", 2),
        "end": (2, "ended", 3),
        "attend": (2, "accepted", 3),
    }[operation]
    actor_id = PROPOSER_ID if operation in {"propose", "withdraw", "end"} else INVITEE_ID
    invitation = _invitation(status, revision)
    return RelationInvitationChangedEvent(
        event_id=invitation_event_id(COMMAND_ID, RULESET, operation, INVITATION_ID),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=actor_id,
        ruleset_version=RULESET,
        operation=operation,
        expected_revision=expected_revision,
        resulting_invitation=invitation,
        visibility=participant_visibility(PROPOSER_ID, INVITEE_ID),
    )


@pytest.mark.parametrize(
    ("operation", "status", "expected_revision", "result_revision", "actor_id"),
    [
        ("propose", "pending", 0, 1, PROPOSER_ID),
        ("accept", "accepted", 1, 2, INVITEE_ID),
        ("decline", "declined", 1, 2, INVITEE_ID),
        ("withdraw", "withdrawn", 1, 2, PROPOSER_ID),
        ("end", "ended", 2, 3, PROPOSER_ID),
        ("attend", "accepted", 2, 3, INVITEE_ID),
    ],
)
def test_each_operation_has_exact_status_revision_actor_and_private_audience(
    operation, status, expected_revision, result_revision, actor_id,
):
    event = _event(operation)

    assert event.type == RELATIONSHIP_INVITATION_CHANGED_EVENT_TYPE
    assert event.operation == operation
    assert event.expected_revision == expected_revision
    assert event.actor_id == actor_id
    assert event.resulting_invitation.status == status
    assert event.resulting_invitation.revision == result_revision
    assert event.visibility == {
        "policy": "subject_ids",
        "subject_ids": sorted((str(PROPOSER_ID), str(INVITEE_ID))),
    }
    assert parse_relationship_invitation_event(event.model_dump(mode="json")) == event
    if operation == "attend":
        assert "requested_by" not in event.model_dump(mode="json")
        assert "work_calendar_checked" not in event.model_dump(mode="json")


def test_end_may_be_recorded_by_either_participant():
    base = _event("end").model_dump(mode="python")
    for actor_id in (PROPOSER_ID, INVITEE_ID):
        assert RelationInvitationChangedEvent.model_validate({**base, "actor_id": actor_id}).actor_id == actor_id


@pytest.mark.parametrize("operation", ["accept", "decline"])
def test_requested_npc_answer_records_proposer_without_changing_actor(operation):
    values = _event(operation).model_dump(mode="python")
    event = RelationInvitationChangedEvent.model_validate({**values, "requested_by": PROPOSER_ID})

    assert event.actor_id == INVITEE_ID
    assert event.requested_by == PROPOSER_ID
    assert event.model_dump(mode="json")["requested_by"] == str(PROPOSER_ID)


def test_requested_npc_attendance_records_the_other_participant_without_changing_actor():
    values = _event("attend").model_dump(mode="python")
    event = RelationInvitationChangedEvent.model_validate({**values, "requested_by": PROPOSER_ID})

    assert event.actor_id == INVITEE_ID
    assert event.requested_by == PROPOSER_ID


def test_default_requested_by_preserves_existing_serialized_event_shape():
    event = _event("accept")

    assert event.requested_by is None
    assert "requested_by" not in event.model_dump(mode="python")
    assert "requested_by" not in event.model_dump(mode="json")
    assert "requested_by" not in event.model_dump_json()
    assert "work_calendar_checked" not in event.model_dump(mode="python")
    assert "work_calendar_checked" not in event.model_dump(mode="json")
    assert "work_calendar_checked" not in event.model_dump_json()


def test_only_attendance_can_record_a_successful_work_calendar_check():
    attendance = _event("attend").model_dump(mode="python")
    assert RelationInvitationChangedEvent.model_validate({
        **attendance, "work_calendar_checked": True,
    }).work_calendar_checked is True

    response = _event("accept").model_dump(mode="python")
    with pytest.raises(ValidationError, match="only meeting attendance"):
        RelationInvitationChangedEvent.model_validate({**response, "work_calendar_checked": True})


@pytest.mark.parametrize(
    ("operation", "requested_by"),
    [
        ("accept", INVITEE_ID),
        ("decline", INVITEE_ID),
        ("propose", PROPOSER_ID),
        ("withdraw", PROPOSER_ID),
        ("end", PROPOSER_ID),
        ("attend", INVITEE_ID),
    ],
)
def test_requested_by_is_restricted_to_proposer_requested_invitee_answer(operation, requested_by):
    values = _event(operation).model_dump(mode="python")

    with pytest.raises(ValidationError):
        RelationInvitationChangedEvent.model_validate({**values, "requested_by": requested_by})


def test_requested_npc_attendance_rejects_nonparticipant_requester():
    values = _event("attend").model_dump(mode="python")
    with pytest.raises(ValidationError, match="other invitation participant"):
        RelationInvitationChangedEvent.model_validate({
            **values,
            "requested_by": UUID("70000000-0000-4000-8000-000000000007"),
        })


def test_event_id_is_deterministic_and_bound_to_command_operation_and_invitation():
    first = invitation_event_id(COMMAND_ID, RULESET, "accept", INVITATION_ID)
    assert first == invitation_event_id(COMMAND_ID, RULESET, "accept", INVITATION_ID)
    assert first != invitation_event_id(uuid4(), RULESET, "accept", INVITATION_ID)
    assert first != invitation_event_id(COMMAND_ID, "other.v1", "accept", INVITATION_ID)
    assert first != invitation_event_id(COMMAND_ID, RULESET, "decline", INVITATION_ID)
    assert first != invitation_event_id(COMMAND_ID, RULESET, "accept", uuid4())


@pytest.mark.parametrize(
    ("operation", "field", "value"),
    [
        ("propose", "expected_revision", 1),
        ("accept", "actor_id", PROPOSER_ID),
        ("decline", "actor_id", PROPOSER_ID),
        ("withdraw", "actor_id", INVITEE_ID),
        ("end", "actor_id", UUID("70000000-0000-4000-8000-000000000007")),
        ("attend", "actor_id", PROPOSER_ID),
        ("accept", "visibility", {"policy": "public"}),
        ("accept", "event_id", UUID("80000000-0000-4000-8000-000000000008")),
    ],
)
def test_event_rejects_invalid_actor_revision_identity_or_audience(operation, field, value):
    values = _event(operation).model_dump(mode="python")
    values[field] = value
    with pytest.raises(ValidationError):
        RelationInvitationChangedEvent.model_validate(values)


@pytest.mark.parametrize(
    ("operation", "status", "revision", "expected_revision"),
    [
        ("propose", "accepted", 2, 0),
        ("accept", "declined", 2, 1),
        ("decline", "accepted", 2, 1),
        ("withdraw", "accepted", 2, 1),
        ("end", "withdrawn", 3, 2),
        ("accept", "accepted", 3, 1),
        ("attend", "declined", 2, 1),
    ],
)
def test_event_rejects_operation_result_mismatch(operation, status, revision, expected_revision):
    values = _event(operation).model_dump(mode="python")
    values["expected_revision"] = expected_revision
    values["resulting_invitation"] = _invitation(status, revision)
    values["event_id"] = invitation_event_id(COMMAND_ID, RULESET, operation, INVITATION_ID)
    with pytest.raises(ValidationError):
        RelationInvitationChangedEvent.model_validate(values)


def test_event_visibility_requires_exact_sorted_participant_list():
    event = _event("accept")
    values = event.model_dump(mode="python")
    with pytest.raises(ValidationError, match="only to its two participants"):
        RelationInvitationChangedEvent.model_validate({
            **values,
            "visibility": {"policy": "subject_ids", "subject_ids": [str(INVITEE_ID), str(PROPOSER_ID)]},
        })
    with pytest.raises(ValidationError, match="only to its two participants"):
        RelationInvitationChangedEvent.model_validate({
            **values,
            "visibility": {
                "policy": "subject_ids",
                "subject_ids": sorted((str(PROPOSER_ID), str(INVITEE_ID), str(uuid4()))),
            },
        })


def test_event_rejects_extra_fields_and_helper_rejects_invalid_scope():
    values = _event("propose").model_dump(mode="python")
    with pytest.raises(ValidationError):
        RelationInvitationChangedEvent.model_validate({**values, "unexpected": True})
    with pytest.raises(ValueError, match="UUID command and invitation"):
        invitation_event_id("bad", RULESET, "propose", INVITATION_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        invitation_event_id(COMMAND_ID, " ", "propose", INVITATION_ID)
    with pytest.raises(ValueError, match="operation is unsupported"):
        invitation_event_id(COMMAND_ID, RULESET, "unknown", INVITATION_ID)  # type: ignore[arg-type]
