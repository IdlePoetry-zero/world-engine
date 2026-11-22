from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.relationship_disclosure_events import (
    DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND,
    RELATIONSHIP_DISCLOSURE_RECORDED_EVENT_TYPE,
    RelationshipDisclosureRecordedEvent,
    parse_relationship_disclosure_event,
    plan_relationship_disclosure,
    relationship_disclosure_event_id,
    relationship_disclosure_record_id,
    relationship_disclosure_visibility,
    validate_relationship_disclosure_batch,
)
from world_engine.domain.relationship_partnerships import RelationshipPartnership


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
DISCLOSER_ID = UUID("40000000-0000-4000-8000-000000000004")
PARTNER_ID = UUID("50000000-0000-4000-8000-000000000005")
RECIPIENT_ID = UUID("60000000-0000-4000-8000-000000000006")
LOCATION_ID = UUID("70000000-0000-4000-8000-000000000007")
PARTNERSHIP_ID = UUID("80000000-0000-4000-8000-000000000008")
ACTIVATION_EVENT_ID = UUID("90000000-0000-4000-8000-000000000009")
SECOND_PARTNER_ID = UUID("a0000000-0000-4000-8000-00000000000a")
SECOND_PARTNERSHIP_ID = UUID("b0000000-0000-4000-8000-00000000000b")
RULESET = "relationship-disclosure-test.v1"
TICK = 42
REVISION = 5


def _state(
    *,
    status: str = "active",
    revision: int = REVISION,
    recipient_hidden: bool = False,
    knowledge: list | None = None,
) -> WorldState:
    hidden = {"hidden": True} if recipient_hidden else {}
    source = RelationshipPartnership(
        partnership_id=PARTNERSHIP_ID,
        proposer_id=DISCLOSER_ID,
        partner_id=PARTNER_ID,
        status=status,
        proposed_tick=3,
        updated_tick=12,
        revision=revision,
    )
    return WorldState(
        current_tick=TICK,
        entities={
            LOCATION_ID: WorldEntity(id=LOCATION_ID, kind="place", name="Atrium"),
            DISCLOSER_ID: WorldEntity(id=DISCLOSER_ID, kind="character", location_id=LOCATION_ID, name="Mara"),
            PARTNER_ID: WorldEntity(id=PARTNER_ID, kind="character", location_id=LOCATION_ID, name="Tavi"),
            RECIPIENT_ID: WorldEntity(
                id=RECIPIENT_ID,
                kind="character",
                location_id=LOCATION_ID,
                name="Ren",
                components=hidden,
            ),
        },
        relationship_partnerships=[source],
        knowledge=knowledge or [],
    )


def _plan(state: WorldState | None = None, *, actor_id: UUID = DISCLOSER_ID, **overrides):
    values = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "ruleset_version": RULESET,
        "actor_id": actor_id,
        "partnership_id": PARTNERSHIP_ID,
        "expected_revision": REVISION,
        "recipient_id": RECIPIENT_ID,
    }
    return plan_relationship_disclosure(state or _state(), **(values | overrides))


def _command_payload(**overrides):
    return {
        "kind": "disclose_relationship_partnership",
        "partnership_id": str(PARTNERSHIP_ID),
        "expected_revision": REVISION,
        "recipient_id": str(RECIPIENT_ID),
    } | overrides


def _scope(**overrides):
    return {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": DISCLOSER_ID,
        "ruleset_version": RULESET,
    } | overrides


def test_planner_makes_one_private_report_without_changing_relationship_or_clock():
    before = _state()
    event = _plan(before)
    receipt = event.record.relationship_disclosure

    assert event.type == RELATIONSHIP_DISCLOSURE_RECORDED_EVENT_TYPE
    assert event.actor_id == DISCLOSER_ID
    assert receipt is not None
    assert receipt.source_partnership_id == PARTNERSHIP_ID
    assert receipt.source_partnership_id != ACTIVATION_EVENT_ID
    assert receipt.source_revision == REVISION
    assert receipt.discloser_id == DISCLOSER_ID
    assert receipt.opposite_participant_id == PARTNER_ID
    assert receipt.recipient_id == RECIPIENT_ID
    assert receipt.location_id == LOCATION_ID
    assert receipt.disclosed_tick == TICK
    assert event.record.holder_id == RECIPIENT_ID
    assert event.record.owner_id is None
    assert event.record.subject_id == DISCLOSER_ID
    assert event.record.kind == "claim"
    assert event.record.fact is None
    assert event.record.claim == f"I am in an active relationship with {PARTNER_ID}."
    assert event.record.source_event_id == event.event_id
    assert event.record.visibility.policy == "subject_ids"
    assert event.record.visibility.subject_ids == [RECIPIENT_ID]
    assert event.visibility == relationship_disclosure_visibility(RECIPIENT_ID)
    assert before.relationship_partnerships == _state().relationship_partnerships
    assert before.knowledge == []
    assert before.current_tick == TICK

    after = apply_event(before, event)
    assert after.knowledge == [event.record]
    assert after.relationship_partnerships == before.relationship_partnerships
    assert after.current_tick == before.current_tick


def test_event_ids_are_deterministic_from_command_and_exact_relationship_revision():
    expected_event = uuid5(
        COMMAND_ID,
        f"{RULESET}:relationship_disclosure.recorded:{PARTNERSHIP_ID}:{REVISION}:{RECIPIENT_ID}",
    )
    expected_record = uuid5(
        COMMAND_ID,
        f"{RULESET}:relationship_disclosure.knowledge:{PARTNERSHIP_ID}:{REVISION}:{RECIPIENT_ID}",
    )
    event = _plan()

    assert relationship_disclosure_event_id(COMMAND_ID, RULESET, PARTNERSHIP_ID, REVISION, RECIPIENT_ID) == expected_event
    assert relationship_disclosure_record_id(COMMAND_ID, RULESET, PARTNERSHIP_ID, REVISION, RECIPIENT_ID) == expected_record
    assert event.event_id == expected_event
    assert event.record.id == expected_record
    assert event.event_id != ACTIVATION_EVENT_ID
    assert event.event_id == relationship_disclosure_event_id(COMMAND_ID, RULESET, PARTNERSHIP_ID, REVISION, RECIPIENT_ID)
    assert event.event_id != relationship_disclosure_event_id(uuid4(), RULESET, PARTNERSHIP_ID, REVISION, RECIPIENT_ID)
    assert event.event_id != relationship_disclosure_event_id(COMMAND_ID, "other.v1", PARTNERSHIP_ID, REVISION, RECIPIENT_ID)
    assert event.event_id != relationship_disclosure_event_id(COMMAND_ID, RULESET, PARTNERSHIP_ID, REVISION + 1, RECIPIENT_ID)
    assert event.event_id != relationship_disclosure_event_id(COMMAND_ID, RULESET, PARTNERSHIP_ID, REVISION, uuid4())


def test_event_round_trips_through_typed_registry_and_receipt_is_immutable():
    event = _plan()
    assert parse_relationship_disclosure_event(event.model_dump(mode="json")) == event
    assert parse_event(event.model_dump(mode="json")) == event
    assert event.record.relationship_disclosure is not None
    with pytest.raises(ValidationError):
        event.record.relationship_disclosure.source_revision = REVISION + 1
    with pytest.raises(ValidationError):
        event.actor_id = uuid4()


def test_batch_validation_binds_exact_standalone_command_and_replans_source():
    event = _plan()
    validate_relationship_disclosure_batch(
        [event], state=_state(), command_type=DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND,
        command_payload=_command_payload(), **_scope(),
    )
    # Replay has the persisted command context but not the original normalized request payload.
    validate_relationship_disclosure_batch([event], state=_state(), **_scope())

    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_relationship_disclosure_batch(
            [event, event], state=_state(), command_type=DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND,
            command_payload=_command_payload(), **_scope(),
        )
    for forged_payload in (
        _command_payload(partnership_id=str(uuid4())),
        _command_payload(expected_revision=REVISION - 1),
        _command_payload(recipient_id=str(uuid4())),
        _command_payload(extra=True),
        {key: value for key, value in _command_payload().items() if key != "recipient_id"},
    ):
        with pytest.raises(InvalidEvent, match="does not match its command"):
            validate_relationship_disclosure_batch(
                [event], state=_state(), command_type=DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND,
                command_payload=forged_payload, **_scope(),
            )
    with pytest.raises(InvalidEvent, match="requires its typed event"):
        validate_relationship_disclosure_batch(
            [], state=_state(), command_type=DISCLOSE_RELATIONSHIP_PARTNERSHIP_COMMAND,
            command_payload=_command_payload(), **_scope(),
        )


@pytest.mark.parametrize(
    ("state", "overrides"),
    [
        (_state(status="ended"), {}),
        (_state(revision=REVISION - 1), {}),
        (_state(recipient_hidden=True), {}),
        (_state(), {"recipient_id": PARTNER_ID}),
        (_state(), {"recipient_id": DISCLOSER_ID}),
        (_state(), {"actor_id": RECIPIENT_ID}),
    ],
)
def test_planner_rejects_inactive_stale_hidden_or_nonthirdparty_disclosure(state, overrides):
    with pytest.raises(InvalidEvent, match="not available"):
        _plan(state, **overrides)


def test_each_partner_can_make_an_independent_statement_but_duplicate_receipt_is_rejected():
    first = _plan()
    after_first = apply_event(_state(), first)
    reverse = _plan(
        after_first,
        actor_id=PARTNER_ID,
        command_id=uuid4(),
    )
    assert reverse.record.relationship_disclosure is not None
    assert reverse.record.relationship_disclosure.discloser_id == PARTNER_ID
    assert reverse.record.relationship_disclosure.opposite_participant_id == DISCLOSER_ID

    with pytest.raises(InvalidEvent, match="not available"):
        _plan(after_first)

    # An unrelated actor gets the same rejection even when the recipient has
    # already received a matching receipt; this cannot probe another's record.
    with pytest.raises(InvalidEvent) as unrelated:
        _plan(after_first, actor_id=RECIPIENT_ID)
    with pytest.raises(InvalidEvent) as fresh_unrelated:
        _plan(_state(), actor_id=RECIPIENT_ID)
    assert str(unrelated.value) == str(fresh_unrelated.value) == "Relationship disclosure is not available."


def test_disclosure_receipt_uses_the_selected_relationship_among_multiple_active_partnerships():
    second_partnership = RelationshipPartnership(
        partnership_id=SECOND_PARTNERSHIP_ID,
        proposer_id=DISCLOSER_ID,
        partner_id=SECOND_PARTNER_ID,
        status="active",
        proposed_tick=4,
        updated_tick=13,
        revision=2,
    )
    multiple_state = _state().model_copy(update={
        "entities": {
            **_state().entities,
            SECOND_PARTNER_ID: WorldEntity(
                id=SECOND_PARTNER_ID,
                kind="character",
                location_id=LOCATION_ID,
                name="Ari",
            ),
        },
        "relationship_partnerships": [*_state().relationship_partnerships, second_partnership],
    })

    event = _plan(
        multiple_state,
        command_id=uuid4(),
        partnership_id=SECOND_PARTNERSHIP_ID,
        expected_revision=2,
    )

    assert event.record.relationship_disclosure is not None
    assert event.record.relationship_disclosure.source_partnership_id == SECOND_PARTNERSHIP_ID
    assert event.record.relationship_disclosure.opposite_participant_id == SECOND_PARTNER_ID
    assert event.record.claim == f"I am in an active relationship with {SECOND_PARTNER_ID}."


def test_tampered_receipt_or_fixed_claim_cannot_be_committed():
    event = _plan()
    values = deepcopy(event.model_dump(mode="python"))
    values["record"]["relationship_disclosure"]["source_partnership_id"] = str(ACTIVATION_EVENT_ID)
    with pytest.raises(ValidationError):
        RelationshipDisclosureRecordedEvent.model_validate(values)

    values = deepcopy(event.model_dump(mode="python"))
    values["record"]["claim"] = "Their partner approves this relationship."
    with pytest.raises(ValidationError):
        RelationshipDisclosureRecordedEvent.model_validate(values)

    with pytest.raises(InvalidEvent, match="schema validation"):
        apply_event(_state(), event.model_copy(update={"record": event.record.model_copy(update={"claim": "Their partner approves this relationship."})}))


def test_generic_knowledge_event_cannot_bypass_typed_relationship_disclosure_validation():
    event = _plan()
    with pytest.raises(ValidationError, match="dedicated typed event"):
        KnowledgeAcquiredEvent(record=event.record)
