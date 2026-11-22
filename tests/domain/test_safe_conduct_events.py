from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.safe_conduct import SafeConductTreaty
from world_engine.domain.safe_conduct_events import (
    SAFE_CONDUCT_CHANGED_EVENT_TYPE,
    SafeConductChangedEvent,
    parse_safe_conduct_event,
    safe_conduct_event_id,
)


WORLD_ID = UUID("11000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("22000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("33000000-0000-4000-8000-000000000003")
PROPOSER_ID = UUID("44000000-0000-4000-8000-000000000004")
RECIPIENT_ID = UUID("55000000-0000-4000-8000-000000000005")
TREATY_ID = UUID("66000000-0000-4000-8000-000000000006")
RULESET = "safe-conduct-test.v1"


def _treaty(*, status="pending", revision=1):
    return SafeConductTreaty(
        treaty_id=TREATY_ID,
        proposer_organization_id=UUID("77000000-0000-4000-8000-000000000007"),
        recipient_organization_id=UUID("88000000-0000-4000-8000-000000000008"),
        proposer_actor_id=PROPOSER_ID,
        recipient_actor_id=RECIPIENT_ID,
        route_id=UUID("99000000-0000-4000-8000-000000000009"),
        from_place_id=UUID("aa000000-0000-4000-8000-00000000000a"),
        to_place_id=UUID("bb000000-0000-4000-8000-00000000000b"),
        opened_tick=100,
        updated_tick=100 if revision == 1 else 110,
        expires_at_tick=1_000,
        proposer_governance_revision=2,
        recipient_governance_revision=3,
        revision=revision,
        status=status,
    )


def _event(operation, treaty, *, expected_revision, actor_id):
    return SafeConductChangedEvent(
        event_id=safe_conduct_event_id(COMMAND_ID, RULESET, operation, TREATY_ID),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=actor_id,
        ruleset_version=RULESET,
        expected_revision=expected_revision,
        operation=operation,
        resulting_treaty=treaty,
    )


@pytest.mark.parametrize(
    ("operation", "status", "expected_revision", "revision", "actor_id"),
    [
        ("propose", "pending", 0, 1, PROPOSER_ID),
        ("ratify", "active", 1, 2, RECIPIENT_ID),
        ("decline", "declined", 1, 2, RECIPIENT_ID),
        # These current-authority checks belong to the pure transition/host.
        ("withdraw", "withdrawn", 1, 2, uuid4()),
        ("revoke", "revoked", 1, 2, uuid4()),
    ],
)
def test_event_binds_status_revision_actor_and_author_only_visibility(
    operation, status, expected_revision, revision, actor_id,
):
    event = _event(
        operation,
        _treaty(status=status, revision=revision),
        expected_revision=expected_revision,
        actor_id=actor_id,
    )

    assert event.type == SAFE_CONDUCT_CHANGED_EVENT_TYPE
    assert event.event_id == uuid5(
        COMMAND_ID,
        f"{RULESET}:safe_conduct.{operation}:{TREATY_ID}",
    )
    assert event.expected_revision == expected_revision
    assert event.visibility == {"policy": "author_only"}
    assert parse_safe_conduct_event(event.model_dump(mode="json")) == event


def test_event_ids_are_deterministic_and_bound_to_command_ruleset_operation_and_treaty():
    first = safe_conduct_event_id(COMMAND_ID, RULESET, "ratify", TREATY_ID)
    assert first == safe_conduct_event_id(COMMAND_ID, RULESET, "ratify", TREATY_ID)
    assert first != safe_conduct_event_id(uuid4(), RULESET, "ratify", TREATY_ID)
    assert first != safe_conduct_event_id(COMMAND_ID, "other.v1", "ratify", TREATY_ID)
    assert first != safe_conduct_event_id(COMMAND_ID, RULESET, "decline", TREATY_ID)
    assert first != safe_conduct_event_id(COMMAND_ID, RULESET, "ratify", uuid4())


@pytest.mark.parametrize(
    ("operation", "status", "expected_revision", "revision", "actor_id", "error"),
    [
        ("propose", "pending", 0, 1, RECIPIENT_ID, "captured proposer"),
        ("propose", "active", 0, 1, PROPOSER_ID, "status does not match"),
        ("propose", "pending", 0, 2, PROPOSER_ID, "create revision one"),
        ("ratify", "active", 1, 2, PROPOSER_ID, "captured recipient"),
        ("decline", "pending", 1, 2, RECIPIENT_ID, "status does not match"),
        ("withdraw", "withdrawn", 1, 3, PROPOSER_ID, "advance exactly one revision"),
        ("revoke", "active", 1, 2, RECIPIENT_ID, "status does not match"),
    ],
)
def test_event_rejects_invalid_status_revision_or_captured_actor(
    operation, status, expected_revision, revision, actor_id, error,
):
    with pytest.raises(ValidationError, match=error):
        _event(
            operation,
            _treaty(status=status, revision=revision),
            expected_revision=expected_revision,
            actor_id=actor_id,
        )


def test_event_rejects_wrong_identity_visibility_extra_fields_and_invalid_revision():
    valid = _event("propose", _treaty(), expected_revision=0, actor_id=PROPOSER_ID)
    values = valid.model_dump(mode="python")

    with pytest.raises(ValidationError, match="bound to its command"):
        SafeConductChangedEvent.model_validate({**values, "event_id": uuid4()})
    with pytest.raises(ValidationError, match="author_only"):
        SafeConductChangedEvent.model_validate({**values, "visibility": {"policy": "public"}})
    with pytest.raises(ValidationError):
        SafeConductChangedEvent.model_validate({**values, "unexpected": True})
    with pytest.raises(ValidationError):
        SafeConductChangedEvent.model_validate({**values, "expected_revision": -1})


def test_event_id_helper_rejects_invalid_scope():
    with pytest.raises(ValueError, match="UUID command and treaty"):
        safe_conduct_event_id("bad", RULESET, "propose", TREATY_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        safe_conduct_event_id(COMMAND_ID, " ", "propose", TREATY_ID)
    with pytest.raises(ValueError, match="unsupported"):
        safe_conduct_event_id(COMMAND_ID, RULESET, "renew", TREATY_ID)  # type: ignore[arg-type]
