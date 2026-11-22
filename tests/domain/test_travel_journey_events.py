from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.travel_journeys import TravelJourney, delay_travel_journey
from world_engine.domain.travel_journey_events import (
    TRAVEL_JOURNEY_CHANGED_EVENT_TYPE,
    TravelJourneyChangedEvent,
    parse_travel_journey_event,
    travel_event_id,
    travel_journey_visibility,
    validate_travel_journey_scope,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
JOURNEY_ID = UUID("50000000-0000-4000-8000-000000000005")
ROUTE_ID = UUID("60000000-0000-4000-8000-000000000006")
FROM_PLACE_ID = UUID("70000000-0000-4000-8000-000000000007")
TO_PLACE_ID = UUID("80000000-0000-4000-8000-000000000008")
RULESET = "travel-test.v1"


def _journey(operation: str, **overrides) -> TravelJourney:
    values = {
        "journey_id": JOURNEY_ID,
        "actor_id": ACTOR_ID,
        "route_id": ROUTE_ID,
        "from_place_id": FROM_PLACE_ID,
        "to_place_id": TO_PLACE_ID,
        "departed_tick": 10,
        "arrival_tick": 30,
        "hazard_tick": 20 if operation == "interrupt" else None,
        "status": {
            "depart": "in_transit",
            "arrive": "arrived",
            "interrupt": "interrupted",
        }[operation],
        "resolved_tick": {
            "depart": None,
            "arrive": 30,
            "interrupt": 20,
        }[operation],
    }
    return TravelJourney.model_validate(values | overrides)


def _event(operation: str, **overrides) -> TravelJourneyChangedEvent:
    journey = _journey(operation)
    values = {
        "event_id": travel_event_id(COMMAND_ID, RULESET, operation, JOURNEY_ID),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": ACTOR_ID,
        "ruleset_version": RULESET,
        "operation": operation,
        "journey": journey,
        "visibility": travel_journey_visibility(ACTOR_ID),
    }
    return TravelJourneyChangedEvent.model_validate(values | overrides)


def _scope(**overrides):
    values = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "ruleset_version": RULESET,
    }
    return values | overrides


@pytest.mark.parametrize(
    ("operation", "status", "resolved_tick"),
    [("depart", "in_transit", None), ("arrive", "arrived", 30), ("interrupt", "interrupted", 20)],
)
def test_event_binds_lifecycle_state_actor_and_private_audience(operation, status, resolved_tick):
    event = _event(operation)

    assert event.type == TRAVEL_JOURNEY_CHANGED_EVENT_TYPE
    assert event.journey.status == status
    assert event.journey.resolved_tick == resolved_tick
    assert event.actor_id == event.journey.actor_id == ACTOR_ID
    assert event.visibility == {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}
    assert parse_travel_journey_event(event.model_dump(mode="json")) == event
    with pytest.raises(ValidationError):
        event.actor_id = uuid4()


def test_travel_event_id_is_deterministic_and_bound_to_command_transition_and_journey():
    event_id = travel_event_id(COMMAND_ID, RULESET, "depart", JOURNEY_ID)

    assert event_id == uuid5(COMMAND_ID, f"{RULESET}:travel_journey.depart:{JOURNEY_ID}")
    assert event_id == travel_event_id(COMMAND_ID, RULESET, "depart", JOURNEY_ID)
    assert event_id != travel_event_id(uuid4(), RULESET, "depart", JOURNEY_ID)
    assert event_id != travel_event_id(COMMAND_ID, "other.v1", "depart", JOURNEY_ID)
    assert event_id != travel_event_id(COMMAND_ID, RULESET, "arrive", JOURNEY_ID)
    assert event_id != travel_event_id(COMMAND_ID, RULESET, "depart", uuid4())
    with pytest.raises(ValueError, match="UUID command and journey"):
        travel_event_id("bad", RULESET, "depart", JOURNEY_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        travel_event_id(COMMAND_ID, " ", "depart", JOURNEY_ID)
    with pytest.raises(ValueError, match="operation is unsupported"):
        travel_event_id(COMMAND_ID, RULESET, "unknown", JOURNEY_ID)  # type: ignore[arg-type]


def test_stationary_window_preserves_the_remaining_distance_to_an_unseen_hazard():
    journey = _journey("depart", hazard_tick=20)
    delayed = delay_travel_journey(journey, 15, 55)
    assert (delayed.hazard_tick, delayed.arrival_tick, delayed.stationary_ticks) == (60, 70, 40)
    assert delay_travel_journey(delayed, 57, 60).hazard_tick == 63
    with pytest.raises(InvalidEvent, match="current stationary window"):
        delay_travel_journey(delayed, 60, 61)


@pytest.mark.parametrize(
    ("operation", "field", "value"),
    [
        ("depart", "actor_id", uuid4()),
        ("depart", "event_id", uuid4()),
        ("depart", "visibility", {"policy": "public"}),
        ("depart", "visibility", {"policy": "subject_ids", "subject_ids": []}),
        ("depart", "visibility", {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID), str(uuid4())]}),
        ("arrive", "unexpected", True),
    ],
)
def test_event_rejects_actor_identity_visibility_and_extra_field_forgery(operation, field, value):
    values = _event(operation).model_dump(mode="python")
    values[field] = value

    with pytest.raises(ValidationError):
        TravelJourneyChangedEvent.model_validate(values)


@pytest.mark.parametrize(
    ("operation", "status"),
    [("depart", "arrived"), ("arrive", "in_transit"), ("interrupt", "arrived")],
)
def test_event_rejects_operation_and_journey_state_mismatch(operation, status):
    journey = _journey("arrive" if status == "arrived" else "depart")
    with pytest.raises(ValidationError, match="state must match its operation"):
        _event(operation, journey=journey)


def test_scope_validator_checks_each_journey_without_requiring_a_standalone_batch():
    depart = _event("depart")
    other_journey_id = uuid4()
    arrive = _event("arrive", event_id=travel_event_id(COMMAND_ID, RULESET, "arrive", other_journey_id),
                    journey=_journey("arrive", journey_id=other_journey_id))

    validate_travel_journey_scope([depart, object()], **_scope())
    validate_travel_journey_scope([depart, arrive], **_scope())
    validate_travel_journey_scope([object()], **_scope())

    wrong_command_id = uuid4()
    for forged in (
        _event("depart", world_id=uuid4()),
        _event("depart", branch_id=uuid4()),
        _event("depart", command_id=wrong_command_id,
               event_id=travel_event_id(wrong_command_id, RULESET, "depart", JOURNEY_ID)),
        _event("depart", ruleset_version="other.v1",
               event_id=travel_event_id(COMMAND_ID, "other.v1", "depart", JOURNEY_ID)),
    ):
        with pytest.raises(InvalidEvent, match="trusted command scope"):
            validate_travel_journey_scope([forged], **_scope())
