from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.application.itinerary_chat import (
    MAX_ITINERARY_ROUTE_IDS,
    MAX_SAFE_INTEGER,
    QueueItineraryIntent,
    build_itinerary_chat_plan,
    itinerary_message_key,
    itinerary_queue_reply,
)
from world_engine.domain.components import CapabilitiesComponent, PlaceComponent, PlaceRoute
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import AccessScope, JobRecord, JobStatus, WorldEntity, WorldState


ACTOR_ID = UUID("10000000-0000-4000-8000-000000000001")
ORIGIN_ID = UUID("20000000-0000-4000-8000-000000000002")
MIDDLE_ID = UUID("30000000-0000-4000-8000-000000000003")
DESTINATION_ID = UUID("40000000-0000-4000-8000-000000000004")
ROUTE_1_ID = UUID("50000000-0000-4000-8000-000000000005")
ROUTE_2_ID = UUID("60000000-0000-4000-8000-000000000006")
ITINERARY_ID = UUID("70000000-0000-4000-8000-000000000007")


def _route_rows(state: WorldState) -> list[dict[str, str]]:
    rows = []
    for origin_id, destination_id, route_id in (
        (ORIGIN_ID, MIDDLE_ID, ROUTE_1_ID),
        (MIDDLE_ID, DESTINATION_ID, ROUTE_2_ID),
    ):
        origin = state.entities[origin_id]
        destination = state.entities[destination_id]
        route = next(
            item for item in PlaceComponent.model_validate(origin.components["place"]).routes
            if item.route_id == route_id
        )
        rows.append({
            "route_id": str(route.route_id),
            "from_place_id": str(origin.id),
            "from_place_name": origin.name or str(origin.id),
            "to_place_id": str(destination.id),
            "to_place_name": destination.name or str(destination.id),
            "travel_ticks": str(route.travel_ticks),
        })
    return rows


def _state(*, can_travel: bool = True, current_tick: int = 100, day_seconds: int = 1_000) -> WorldState:
    grants = ["world.travel"] if can_travel else []
    return WorldState(
        current_tick=current_tick,
        calendar={"day_seconds": day_seconds, "year_days": 360},
        entities={
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                name="Traveler",
                location_id=ORIGIN_ID,
                components={"capabilities": CapabilitiesComponent(grants=grants).model_dump(mode="json")},
            ),
            ORIGIN_ID: WorldEntity(
                id=ORIGIN_ID,
                kind="place",
                name="Home Port",
                components={"place": PlaceComponent(routes=[PlaceRoute(
                    route_id=ROUTE_1_ID,
                    from_place_id=ORIGIN_ID,
                    to_place_id=MIDDLE_ID,
                    travel_ticks=10,
                )]).model_dump(mode="json")},
            ),
            MIDDLE_ID: WorldEntity(
                id=MIDDLE_ID,
                kind="place",
                name="Known Waypoint",
                components={"place": PlaceComponent(routes=[PlaceRoute(
                    route_id=ROUTE_2_ID,
                    from_place_id=MIDDLE_ID,
                    to_place_id=DESTINATION_ID,
                    travel_ticks=20,
                )]).model_dump(mode="json")},
            ),
            DESTINATION_ID: WorldEntity(
                id=DESTINATION_ID,
                kind="place",
                name="Public Harbor",
                components={"place": PlaceComponent().model_dump(mode="json")},
            ),
        },
    )


def _action(
    *,
    route_ids: tuple[UUID, ...] = (ROUTE_1_ID, ROUTE_2_ID),
    amount: int = 1,
    unit: str = "month",
    auto_care: bool = False,
) -> QueueItineraryIntent:
    return QueueItineraryIntent(
        kind="queue_itinerary",
        route_ids=route_ids,
        amount=amount,
        unit=unit,  # type: ignore[arg-type]
        itinerary_auto_care=auto_care,
    )


def _plan(state=None, action=None, *, rows=None):
    state = state or _state()
    return build_itinerary_chat_plan(
        state,
        ACTOR_ID,
        action or _action(),
        itinerary_id=ITINERARY_ID,
        visible_routes=_route_rows(state) if rows is None else rows,
    )


def test_queue_itinerary_intent_has_bounded_ordered_routes_safe_amount_and_strict_care_flag():
    intent = QueueItineraryIntent.model_validate({
        "kind": "queue_itinerary",
        "route_ids": [str(ROUTE_1_ID), str(ROUTE_2_ID)],
        "amount": 2,
        "unit": "month",
    })

    assert intent.route_ids == (ROUTE_1_ID, ROUTE_2_ID)
    assert intent.itinerary_auto_care is False
    with pytest.raises(ValidationError):
        QueueItineraryIntent.model_validate({
            "kind": "queue_itinerary", "route_ids": [], "amount": 1, "unit": "day",
        })
    with pytest.raises(ValidationError):
        QueueItineraryIntent.model_validate({
            "kind": "queue_itinerary", "route_ids": [str(ROUTE_1_ID)], "amount": True, "unit": "day",
        })
    with pytest.raises(ValidationError):
        QueueItineraryIntent.model_validate({
            "kind": "queue_itinerary", "route_ids": [str(ROUTE_1_ID)],
            "amount": MAX_SAFE_INTEGER + 1, "unit": "day",
        })
    with pytest.raises(ValidationError):
        QueueItineraryIntent.model_validate({
            "kind": "queue_itinerary", "route_ids": [str(ROUTE_1_ID)],
            "amount": 1, "unit": "day", "itinerary_auto_care": "true",
        })
    with pytest.raises(ValidationError):
        QueueItineraryIntent.model_validate({
            "kind": "queue_itinerary", "route_ids": [str(ROUTE_1_ID)] * (MAX_ITINERARY_ROUTE_IDS + 1),
            "amount": 1, "unit": "day",
        })


@pytest.mark.parametrize(
    ("amount", "unit", "expected_duration"),
    [
        (30, "second", 30),
        (2, "hour", 2 * 60 * 60),
        (2, "day", 2 * 1_000),
        (2, "month", 2 * 30 * 1_000),
        (2, "year", 2 * 360 * 1_000),
    ],
)
def test_plan_uses_explicit_connected_visible_routes_and_calendar_duration(amount, unit, expected_duration):
    state = _state()
    plan = _plan(state, _action(amount=amount, unit=unit, auto_care=True))

    assert plan.activity == "travel"
    assert plan.actor_id == ACTOR_ID
    assert plan.started_tick == 100
    assert plan.target_tick == 100 + expected_duration
    assert plan.travel_itinerary is not None
    assert [leg.route_id for leg in plan.travel_itinerary.legs] == [ROUTE_1_ID, ROUTE_2_ID]
    assert plan.travel_itinerary.destination_place_id == DESTINATION_ID
    assert plan.itinerary_auto_care is True
    assert plan.depart_immediately is True
    assert plan.route_id is None
    assert plan.constraints == ()
    assert plan.stop_conditions == ()


def test_plan_rejects_unknown_or_stale_graph_edges_and_disconnected_order():
    state = _state()
    rows = _route_rows(state)

    with pytest.raises(ValueError, match="visible route options"):
        _plan(state, _action(route_ids=(uuid4(),)), rows=rows)
    mismatched = [rows[0] | {"travel_ticks": "11"}, rows[1]]
    with pytest.raises(ValueError, match="approved visible route row"):
        _plan(state, _action(route_ids=(ROUTE_1_ID,)), rows=mismatched)
    with pytest.raises(InvalidEvent, match="registered at its origin"):
        _plan(state, _action(route_ids=(ROUTE_2_ID, ROUTE_1_ID)), rows=rows)
    with pytest.raises(ValueError, match="unexpected shape"):
        _plan(state, _action(), rows=[rows[0] | {"risk": "0.5"}, rows[1]])


def test_plan_rechecks_first_leg_permission_and_closed_route_contract():
    state = _state(can_travel=False)
    with pytest.raises(InvalidEvent, match="not currently accessible"):
        _plan(state)

    state = _state()
    origin = state.entities[ORIGIN_ID]
    closed = PlaceRoute(
        route_id=ROUTE_1_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=MIDDLE_ID,
        travel_ticks=10,
        is_open=False,
    )
    state = state.model_copy(update={"entities": state.entities | {
        ORIGIN_ID: origin.model_copy(update={"components": {
            "place": PlaceComponent(routes=[closed]).model_dump(mode="json"),
        }}),
    }})
    with pytest.raises(InvalidEvent, match="not currently accessible"):
        _plan(state)


def test_plan_rejects_safe_duration_clock_overflow_and_route_deadline_preflight():
    state = _state(current_tick=2**63 - 5)
    with pytest.raises(ValueError, match="deadline exceeds"):
        _plan(state, _action(amount=10, unit="second"))

    with pytest.raises(InvalidEvent, match="cannot fit"):
        _plan(_state(), _action(amount=5, unit="second"))


def test_itinerary_message_key_and_queue_receipt_are_stable_and_zero_elapsed():
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request_id = uuid4()
    key = itinerary_message_key(scope, request_id)
    assert key == itinerary_message_key(scope, request_id)
    assert key == f"itinerary-message:{scope.user_id}:{request_id}"

    plan = _plan()
    now = datetime.now(timezone.utc)
    job = JobRecord(
        job_id=uuid4(),
        tenant_id=scope.tenant_id,
        world_id=uuid4(),
        branch_id=uuid4(),
        job_type="simulation.long_action",
        status=JobStatus.QUEUED,
        payload={"plan": plan.model_dump(mode="json")},
        attempt_count=0,
        fencing_token=0,
        created_at=now,
        updated_at=now,
    )

    first = itinerary_queue_reply(request_id, job)
    changed_status = job.model_copy(update={
        "status": JobStatus.RUNNING,
        "attempt_count": 4,
        "committed_through_tick": 200,
    })
    assert itinerary_queue_reply(request_id, changed_status) == first
    assert first["message_id"] == str(request_id)
    assert first["job_id"] == str(job.job_id)
    assert first["elapsed_ticks"] == "0"
    assert first["world_effect"] is False
    assert first["references"] == [str(ROUTE_1_ID), str(ROUTE_2_ID)]

