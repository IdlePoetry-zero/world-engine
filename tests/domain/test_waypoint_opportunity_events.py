from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.components import PlaceComponent, WaypointOpportunity
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.models import ScheduledEventRecord, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash, state_json
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)
from world_engine.domain.waypoint_opportunity_events import (
    WAYPOINT_OPPORTUNITY_COMMAND,
    WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE,
    WaypointOpportunityDecision,
    WaypointOpportunityDecisionRecordedEvent,
    parse_waypoint_opportunity_event,
    validate_waypoint_opportunity_batch,
    waypoint_opportunity_event_id,
    waypoint_opportunity_request_id,
    waypoint_opportunity_visibility,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
JOURNEY_ID = UUID("50000000-0000-4000-8000-000000000005")
OPPORTUNITY_ID = UUID("60000000-0000-4000-8000-000000000006")
FROM_PLACE_ID = UUID("70000000-0000-4000-8000-000000000007")
TO_PLACE_ID = UUID("80000000-0000-4000-8000-000000000008")
RULESET = "waypoint-opportunity-test.v1"
STATE_VERSION = 12
ARRIVAL_TICK = 30


def _state(
    *,
    actor_status: str = "active",
    actor_location_id: UUID = TO_PLACE_ID,
    journey_status: str = "arrived",
    resolved_tick: int | None = ARRIVAL_TICK,
    opportunity: WaypointOpportunity | None = None,
    current_tick: int = ARRIVAL_TICK,
) -> WorldState:
    opportunity = opportunity or WaypointOpportunity(
        opportunity_id=OPPORTUNITY_ID,
        title="Old observatory",
        summary="A narrow path climbs to the old observatory.",
        minimum_itinerary_elapsed_ticks=5,
    )
    journey = TravelJourney(
        journey_id=JOURNEY_ID,
        actor_id=ACTOR_ID,
        route_id=uuid4(),
        from_place_id=FROM_PLACE_ID,
        to_place_id=TO_PLACE_ID,
        departed_tick=10,
        arrival_tick=ARRIVAL_TICK,
        status=journey_status,  # type: ignore[arg-type]
        resolved_tick=resolved_tick,
    )
    schedule = ScheduledEventRecord(
        id=travel_resolution_schedule_id(JOURNEY_ID),
        due_tick=ARRIVAL_TICK,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        status="completed",
        completed_runs=1,
        payload={"journey_id": str(JOURNEY_ID)},
    )
    return WorldState(
        current_tick=current_tick,
        entities={
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                status=actor_status,
                location_id=actor_location_id,
            ),
            FROM_PLACE_ID: WorldEntity(
                id=FROM_PLACE_ID,
                kind="place",
                components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
            ),
            TO_PLACE_ID: WorldEntity(
                id=TO_PLACE_ID,
                kind="place",
                components={"place": PlaceComponent(
                    place_type="site", waypoint_opportunities=[opportunity],
                ).model_dump(mode="json")},
            ),
        },
        travel_journeys=[journey],
        scheduled_events=[schedule],
    )


def _record(**overrides: object) -> WaypointOpportunityDecision:
    return WaypointOpportunityDecision.model_validate({
        "journey_id": JOURNEY_ID,
        "actor_id": ACTOR_ID,
        "opportunity_id": OPPORTUNITY_ID,
        "state_version": STATE_VERSION,
        "at_tick": ARRIVAL_TICK,
        "itinerary_started_tick": 0,
        "choice": "continue",
    } | overrides)


def _event(**overrides: object) -> WaypointOpportunityDecisionRecordedEvent:
    record = _record()
    return WaypointOpportunityDecisionRecordedEvent.model_validate({
        "event_id": waypoint_opportunity_event_id(JOURNEY_ID),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": ACTOR_ID,
        "ruleset_version": RULESET,
        "record": record,
        "visibility": waypoint_opportunity_visibility(ACTOR_ID),
    } | overrides)


def _validate_batch(event: WaypointOpportunityDecisionRecordedEvent, **overrides: object) -> None:
    validate_waypoint_opportunity_batch(
        [event],
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=ACTOR_ID,
        ruleset_version=RULESET,
        command_type=WAYPOINT_OPPORTUNITY_COMMAND,
        command_payload={
            "request_id": str(waypoint_opportunity_request_id(JOURNEY_ID, OPPORTUNITY_ID, STATE_VERSION)),
            "option_id": "continue",
        },
        state_version=STATE_VERSION,
        **overrides,
    )


def test_event_schema_is_registered_private_and_stable_per_journey():
    event = _event()

    assert event.type == WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE
    assert event.type == "core.waypoint_opportunity_decision_recorded.v1"
    assert event.visibility == {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}
    assert parse_waypoint_opportunity_event(event.model_dump(mode="json")) == event
    assert parse_event(event.model_dump(mode="json")) == event
    assert waypoint_opportunity_event_id(JOURNEY_ID) == uuid5(
        JOURNEY_ID, f"{WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE}:event",
    )
    assert waypoint_opportunity_request_id(JOURNEY_ID, OPPORTUNITY_ID, STATE_VERSION) == uuid5(
        JOURNEY_ID,
        f"{WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE}:request:{OPPORTUNITY_ID}:{STATE_VERSION}",
    )
    assert waypoint_opportunity_request_id(JOURNEY_ID, OPPORTUNITY_ID, STATE_VERSION) != waypoint_opportunity_request_id(
        JOURNEY_ID, OPPORTUNITY_ID, STATE_VERSION + 1,
    )
    with pytest.raises(ValidationError):
        WaypointOpportunityDecisionRecordedEvent.model_validate(
            event.model_dump(mode="python") | {"visibility": {"policy": "public"}},
        )


def test_batch_validator_binds_command_scope_pre_state_version_and_exact_payload():
    _validate_batch(_event())
    validate_waypoint_opportunity_batch(
        [_event()],
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=ACTOR_ID,
        ruleset_version=RULESET,
        state_version=STATE_VERSION,
    )

    with pytest.raises(InvalidEvent, match="exact request and option payload"):
        validate_waypoint_opportunity_batch(
            [_event()],
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=COMMAND_ID,
            actor_id=ACTOR_ID,
            ruleset_version=RULESET,
            command_type=WAYPOINT_OPPORTUNITY_COMMAND,
            command_payload={"request_id": str(uuid4()), "option_id": "continue"},
            state_version=STATE_VERSION,
        )
    with pytest.raises(InvalidEvent, match="supplied together"):
        validate_waypoint_opportunity_batch(
            [_event()],
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=COMMAND_ID,
            actor_id=ACTOR_ID,
            ruleset_version=RULESET,
            command_type=WAYPOINT_OPPORTUNITY_COMMAND,
            state_version=STATE_VERSION,
        )
    with pytest.raises(InvalidEvent, match="trusted command scope"):
        validate_waypoint_opportunity_batch(
            [_event()],
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=COMMAND_ID,
            actor_id=ACTOR_ID,
            ruleset_version=RULESET,
            command_type=WAYPOINT_OPPORTUNITY_COMMAND,
            command_payload={
                "request_id": str(waypoint_opportunity_request_id(JOURNEY_ID, OPPORTUNITY_ID, STATE_VERSION)),
                "option_id": "continue",
            },
            state_version=STATE_VERSION + 1,
        )
    with pytest.raises(InvalidEvent, match="requires its typed event"):
        validate_waypoint_opportunity_batch(
            [],
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=COMMAND_ID,
            actor_id=ACTOR_ID,
            ruleset_version=RULESET,
            command_type=WAYPOINT_OPPORTUNITY_COMMAND,
        )


def test_reducer_records_one_available_choice_for_the_actor_at_arrival():
    state = _state()
    event = _event()

    updated = apply_event(state, event)

    assert updated.waypoint_opportunity_decisions == [_record()]
    assert state_json(updated)["waypoint_opportunity_decisions"][0]["choice"] == "continue"
    with pytest.raises(InvalidEvent, match="only one"):
        apply_event(updated, event)


@pytest.mark.parametrize(
    ("state_overrides", "record_overrides", "message"),
    [
        ({"current_tick": ARRIVAL_TICK + 1}, {}, "exact current world tick"),
        ({"actor_status": "dead"}, {}, "active actor"),
        ({"actor_location_id": FROM_PLACE_ID}, {}, "at the arrived destination"),
        ({"journey_status": "interrupted", "resolved_tick": ARRIVAL_TICK}, {}, "actual arrival"),
        ({}, {"itinerary_started_tick": 11}, "actual arrival"),
        (
            {},
            {},
            "registered and available",
        ),
    ],
)
def test_reducer_rejects_nonarrival_inactive_actor_or_unavailable_opportunity(
    state_overrides: dict[str, object],
    record_overrides: dict[str, object],
    message: str,
):
    state = _state(**state_overrides)
    if record_overrides:
        event = _event(record=_record(**record_overrides))
    elif message == "registered and available":
        state = _state(opportunity=WaypointOpportunity(
            opportunity_id=OPPORTUNITY_ID,
            title="Old observatory",
            summary="A narrow path climbs to the old observatory.",
            minimum_itinerary_elapsed_ticks=100,
        ))
        event = _event()
    else:
        event = _event()

    with pytest.raises(InvalidEvent, match=message):
        apply_event(state, event)


def test_empty_decision_registry_keeps_legacy_state_hash_shape():
    state = WorldState()
    payload = state_json(state)
    legacy_payload = dict(payload)
    legacy_payload.pop("waypoint_opportunity_decisions", None)
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        .encode("utf-8")
    ).hexdigest()

    assert "waypoint_opportunity_decisions" not in payload
    assert state_hash(state) == legacy_digest
    assert state_hash(WorldState.model_validate(state.model_dump(mode="json"))) == state_hash(state)
