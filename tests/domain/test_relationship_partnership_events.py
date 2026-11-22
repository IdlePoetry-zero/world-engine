from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.relationship_partnership_events import (
    NPC_PARTNERSHIP_RESPONSE_COMMAND,
    RELATIONSHIP_PARTNERSHIP_CHANGED_EVENT_TYPE,
    RelationshipPartnershipChangedEvent,
    partnership_event_id,
    partnership_visibility,
    parse_relationship_partnership_event,
    validate_relationship_partnership_batch,
)
from world_engine.domain.relationship_partnerships import RelationshipPartnership


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
PROPOSER_ID = UUID("40000000-0000-4000-8000-000000000004")
PARTNER_ID = UUID("50000000-0000-4000-8000-000000000005")
PARTNERSHIP_ID = uuid5(COMMAND_ID, "relationship-partnership")
RULESET = "partnership-test.v1"


def _partnership(operation: str, **overrides) -> RelationshipPartnership:
    status, revision, updated_tick = {
        "propose": ("pending", 1, 10),
        "revise": ("pending", 2, 11),
        "counter": ("pending", 2, 11),
        "accept": ("active", 2, 11),
        "decline": ("declined", 2, 11),
        "withdraw": ("withdrawn", 2, 11),
        "end": ("ended", 3, 12),
    }[operation]
    values = {
        "partnership_id": PARTNERSHIP_ID,
        "proposer_id": PROPOSER_ID,
        "partner_id": PARTNER_ID,
        "status": status,
        "proposed_tick": 10,
        "updated_tick": updated_tick,
        "revision": revision,
    }
    if operation == "revise":
        values.update(proposal_terms=("revised full terms",), proposal_author_id=PROPOSER_ID)
    elif operation == "counter":
        values.update(proposal_terms=("counter full terms",), proposal_author_id=PARTNER_ID)
    return RelationshipPartnership.model_validate(values | overrides)


def _event(operation: str, *, requested_by: UUID | None = None, **overrides) -> RelationshipPartnershipChangedEvent:
    expected_revision = {
        "propose": 0, "revise": 1, "counter": 1,
        "accept": 1, "decline": 1, "withdraw": 1, "end": 2,
    }[operation]
    actor_id = PROPOSER_ID if operation in {"propose", "revise", "withdraw", "end"} else PARTNER_ID
    partnership = _partnership(operation)
    values = {
        "event_id": partnership_event_id(COMMAND_ID, RULESET, operation, partnership.partnership_id),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": actor_id,
        "ruleset_version": RULESET,
        "operation": operation,
        "expected_revision": expected_revision,
        "resulting_partnership": partnership,
        "visibility": partnership_visibility(PROPOSER_ID, PARTNER_ID),
    }
    if requested_by is not None:
        values["requested_by"] = requested_by
    return RelationshipPartnershipChangedEvent.model_validate(values | overrides)


def _scope(**overrides):
    values = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": PROPOSER_ID,
        "ruleset_version": RULESET,
    }
    return values | overrides


@pytest.mark.parametrize(
    ("operation", "status", "revision", "expected_revision", "actor_id"),
    [
        ("propose", "pending", 1, 0, PROPOSER_ID),
        ("revise", "pending", 2, 1, PROPOSER_ID),
        ("counter", "pending", 2, 1, PARTNER_ID),
        ("accept", "active", 2, 1, PARTNER_ID),
        ("decline", "declined", 2, 1, PARTNER_ID),
        ("withdraw", "withdrawn", 2, 1, PROPOSER_ID),
        ("end", "ended", 3, 2, PROPOSER_ID),
    ],
)
def test_each_operation_has_exact_status_revision_actor_and_private_audience(
    operation, status, revision, expected_revision, actor_id,
):
    event = _event(operation)

    assert event.type == RELATIONSHIP_PARTNERSHIP_CHANGED_EVENT_TYPE
    assert event.operation == operation
    assert event.expected_revision == expected_revision
    assert event.actor_id == actor_id
    assert event.resulting_partnership.status == status
    assert event.resulting_partnership.revision == revision
    assert event.visibility == {
        "policy": "subject_ids",
        "subject_ids": sorted((str(PROPOSER_ID), str(PARTNER_ID))),
    }
    assert parse_relationship_partnership_event(event.model_dump(mode="json")) == event
    if operation == "propose":
        assert event.resulting_partnership.partnership_id == uuid5(COMMAND_ID, "relationship-partnership")


def test_partnership_event_is_frozen_and_legacy_json_omits_optional_requester():
    event = _event("accept")
    serialized = event.model_dump(mode="json")

    assert event.requested_by is None
    assert "requested_by" not in event.model_dump(mode="python")
    assert "requested_by" not in serialized
    assert '"requested_by"' not in event.model_dump_json()
    assert "proposal_author_id" not in serialized["resulting_partnership"]
    assert "current_proposal_author_id" not in serialized["resulting_partnership"]
    assert set(serialized) == {
        "type", "event_id", "world_id", "branch_id", "command_id", "actor_id",
        "ruleset_version", "operation", "expected_revision", "resulting_partnership", "visibility",
    }
    with pytest.raises(ValidationError):
        event.actor_id = uuid4()


def test_end_may_be_recorded_by_either_participant():
    for actor_id in (PROPOSER_ID, PARTNER_ID):
        assert _event("end", actor_id=actor_id).actor_id == actor_id


def test_requested_response_records_proposer_and_recipient_separately():
    for operation in ("accept", "decline"):
        event = _event(operation, requested_by=PROPOSER_ID)
        assert event.requested_by == PROPOSER_ID
        assert event.actor_id == PARTNER_ID


def test_event_id_is_deterministic_and_bound_to_command_operation_and_partnership():
    event_id = partnership_event_id(COMMAND_ID, RULESET, "accept", PARTNERSHIP_ID)

    assert event_id == uuid5(COMMAND_ID, f"{RULESET}:relationship_partnership.accept:{PARTNERSHIP_ID}")
    assert event_id == partnership_event_id(COMMAND_ID, RULESET, "accept", PARTNERSHIP_ID)
    assert event_id != partnership_event_id(uuid4(), RULESET, "accept", PARTNERSHIP_ID)
    assert event_id != partnership_event_id(COMMAND_ID, "other.v1", "accept", PARTNERSHIP_ID)
    assert event_id != partnership_event_id(COMMAND_ID, RULESET, "decline", PARTNERSHIP_ID)
    assert event_id != partnership_event_id(COMMAND_ID, RULESET, "accept", uuid4())
    with pytest.raises(ValueError, match="UUID command and partnership"):
        partnership_event_id("bad", RULESET, "accept", PARTNERSHIP_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        partnership_event_id(COMMAND_ID, " ", "accept", PARTNERSHIP_ID)
    with pytest.raises(ValueError, match="operation is unsupported"):
        partnership_event_id(COMMAND_ID, RULESET, "unknown", PARTNERSHIP_ID)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("operation", "field", "value"),
    [
        ("propose", "expected_revision", 1),
        ("accept", "actor_id", PROPOSER_ID),
        ("decline", "actor_id", PROPOSER_ID),
        ("withdraw", "actor_id", PARTNER_ID),
        ("end", "actor_id", uuid4()),
        ("accept", "event_id", uuid4()),
        ("accept", "visibility", {"policy": "public"}),
        ("accept", "visibility", {
            "policy": "subject_ids", "subject_ids": [str(PARTNER_ID), str(PROPOSER_ID)],
        }),
    ],
)
def test_event_rejects_invalid_actor_revision_identity_and_visibility(operation, field, value):
    values = _event(operation).model_dump(mode="python")
    values[field] = value

    with pytest.raises(ValidationError):
        RelationshipPartnershipChangedEvent.model_validate(values)


def test_event_rejects_invalid_requested_by_or_nondeterministic_proposal_id():
    accepted = _event("accept").model_dump(mode="python")
    with pytest.raises(ValidationError, match="requested by the current author"):
        RelationshipPartnershipChangedEvent.model_validate({**accepted, "requested_by": PARTNER_ID})

    for operation in ("propose", "revise", "withdraw", "end"):
        values = _event(operation).model_dump(mode="python")
        with pytest.raises(ValidationError, match="only a partner response or counterproposal"):
            RelationshipPartnershipChangedEvent.model_validate({**values, "requested_by": PROPOSER_ID})

    invalid_counter = _event("counter").model_dump(mode="python")
    with pytest.raises(ValidationError, match="requested by its current recipient"):
        RelationshipPartnershipChangedEvent.model_validate({**invalid_counter, "requested_by": PARTNER_ID})

    proposal = _event("propose").model_dump(mode="python")
    wrong_id = uuid4()
    wrong_partnership = _partnership("propose", partnership_id=wrong_id)
    with pytest.raises(ValidationError, match="derive from its proposal command"):
        RelationshipPartnershipChangedEvent.model_validate({
            **proposal,
            "event_id": partnership_event_id(COMMAND_ID, RULESET, "propose", wrong_id),
            "resulting_partnership": wrong_partnership,
        })


def test_direct_commands_bind_exact_normalized_payloads():
    proposals = (
        ("propose", "action.propose_relationship_partnership", {
            "kind": "propose_relationship_partnership", "partner_id": str(PARTNER_ID),
        }),
        ("revise", "action.update_relationship_partnership_proposal", {
            "kind": "update_relationship_partnership_proposal", "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1, "operation": "revise", "proposal_terms": ["revised full terms"],
        }),
        ("counter", "action.update_relationship_partnership_proposal", {
            "kind": "update_relationship_partnership_proposal", "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1, "operation": "counter", "proposal_terms": ["counter full terms"],
        }),
        ("accept", "action.resolve_relationship_partnership", {
            "kind": "resolve_relationship_partnership", "partnership_id": str(PARTNERSHIP_ID),
            "operation": "accept", "expected_revision": 1,
        }),
        ("decline", "action.resolve_relationship_partnership", {
            "kind": "resolve_relationship_partnership", "partnership_id": str(PARTNERSHIP_ID),
            "operation": "decline", "expected_revision": 1,
        }),
        ("withdraw", "action.resolve_relationship_partnership", {
            "kind": "resolve_relationship_partnership", "partnership_id": str(PARTNERSHIP_ID),
            "operation": "withdraw", "expected_revision": 1,
        }),
        ("end", "action.resolve_relationship_partnership", {
            "kind": "resolve_relationship_partnership", "partnership_id": str(PARTNERSHIP_ID),
            "operation": "end", "expected_revision": 2,
        }),
    )
    for operation, command_type, payload in proposals:
        actor_id = PARTNER_ID if operation in {"accept", "decline", "counter"} else PROPOSER_ID
        validate_relationship_partnership_batch(
            [_event(operation)], command_type=command_type, command_payload=payload,
            **_scope(actor_id=actor_id),
        )
        if operation == "propose":
            forged_payloads = (
                {**payload, "partner_id": str(uuid4())},
                {key: value for key, value in payload.items() if key != "partner_id"},
                {**payload, "extra": True},
            )
        elif operation in {"revise", "counter"}:
            forged_payloads = (
                {**payload, "partnership_id": str(uuid4())},
                {**payload, "expected_revision": payload["expected_revision"] + 1},
                {**payload, "operation": "counter" if operation == "revise" else "revise"},
                {**payload, "proposal_terms": ["different terms"]},
                {**payload, "extra": True},
            )
        else:
            forged_payloads = (
                {**payload, "partnership_id": str(uuid4())},
                {**payload, "expected_revision": payload["expected_revision"] + 1},
                {**payload, "operation": "decline" if operation == "accept" else "accept"},
                {**payload, "extra": True},
            )
        for forged_payload in forged_payloads:
            with pytest.raises(InvalidEvent, match="does not match its command proposal"):
                validate_relationship_partnership_batch(
                    [_event(operation)], command_type=command_type, command_payload=forged_payload,
                    **_scope(actor_id=actor_id),
                )


def test_requested_response_binds_only_partnership_and_expected_revision():
    for operation in ("accept", "decline"):
        event = _event(operation, requested_by=PROPOSER_ID)
        payload = {
            "kind": "request_relationship_partnership_response",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
        }
        validate_relationship_partnership_batch(
            [event], command_type=NPC_PARTNERSHIP_RESPONSE_COMMAND, command_payload=payload,
            **_scope(actor_id=PROPOSER_ID),
        )
        validate_relationship_partnership_batch([event], **_scope(actor_id=PROPOSER_ID))

        for forged_payload in (
            {**payload, "partnership_id": str(uuid4())},
            {**payload, "expected_revision": 2},
            {**payload, "operation": operation},
            {**payload, "extra": True},
        ):
            with pytest.raises(InvalidEvent, match="does not match its command proposal"):
                validate_relationship_partnership_batch(
                    [event], command_type=NPC_PARTNERSHIP_RESPONSE_COMMAND,
                    command_payload=forged_payload, **_scope(actor_id=PROPOSER_ID),
                )
        with pytest.raises(InvalidEvent, match="standalone event"):
            validate_relationship_partnership_batch(
                [event], command_type=None, command_payload=None, **_scope(actor_id=PARTNER_ID),
            )
        with pytest.raises(InvalidEvent, match="standalone event"):
            validate_relationship_partnership_batch(
                [event], command_type=NPC_PARTNERSHIP_RESPONSE_COMMAND,
                command_payload=payload, **_scope(actor_id=PARTNER_ID),
            )


def test_hosted_counterproposal_uses_response_command_and_reverses_current_author():
    event = _event("counter", requested_by=PROPOSER_ID)
    assert event.actor_id == PARTNER_ID
    assert event.resulting_partnership.current_proposal_author_id == PARTNER_ID
    assert event.resulting_partnership.current_proposal_recipient_id == PROPOSER_ID
    payload = {
        "kind": "request_relationship_partnership_response",
        "partnership_id": str(PARTNERSHIP_ID),
        "expected_revision": 1,
    }
    validate_relationship_partnership_batch(
        [event], command_type=NPC_PARTNERSHIP_RESPONSE_COMMAND, command_payload=payload,
        **_scope(actor_id=PROPOSER_ID),
    )
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_relationship_partnership_batch(
            [event], command_type=NPC_PARTNERSHIP_RESPONSE_COMMAND,
            command_payload={**payload, "operation": "counter"}, **_scope(actor_id=PROPOSER_ID),
        )


def test_direct_update_command_requires_full_current_terms_payload():
    event = _event("counter")
    payload = {
        "kind": "update_relationship_partnership_proposal",
        "partnership_id": str(PARTNERSHIP_ID),
        "expected_revision": 1,
        "operation": "counter",
        "proposal_terms": ["counter full terms"],
    }
    validate_relationship_partnership_batch(
        [event], command_type="action.update_relationship_partnership_proposal",
        command_payload=payload, **_scope(actor_id=PARTNER_ID),
    )
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_relationship_partnership_batch(
            [event], command_type="action.update_relationship_partnership_proposal",
            command_payload={**payload, "proposal_terms": ["partial term"]},
            **_scope(actor_id=PARTNER_ID),
        )
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_relationship_partnership_batch(
            [event], command_type="action.update_relationship_partnership_proposal",
            command_payload={**payload, "expected_revision": True},
            **_scope(actor_id=PARTNER_ID),
        )


def test_batch_requires_typed_event_and_rejects_mixed_batches_or_wrong_command():
    for command_type in (
        "action.propose_relationship_partnership",
        "action.resolve_relationship_partnership",
        "action.update_relationship_partnership_proposal",
        NPC_PARTNERSHIP_RESPONSE_COMMAND,
    ):
        with pytest.raises(InvalidEvent, match="requires its typed event"):
            validate_relationship_partnership_batch(
                [], command_type=command_type, command_payload={}, **_scope(),
            )

    event = _event("propose")
    payload = {"kind": "propose_relationship_partnership", "partner_id": str(PARTNER_ID)}
    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_relationship_partnership_batch(
            [event, object()], command_type="action.propose_relationship_partnership",
            command_payload=payload, **_scope(),
        )
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_relationship_partnership_batch(
            [event], command_type="action.resolve_relationship_partnership",
            command_payload=payload, **_scope(),
        )
